"""Stable Avibe-owned MCP server for the desktop computer-use daemon.

Backend processes keep this stdio server for their lifetime. The desktop shell
may stop or replace its Cua daemon independently; this server reads the shared
desktop state on every call and replaces only its short-lived upstream proxy.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

# Managed MCP launches this file as an absolute script with Python isolated
# mode. Put the installed/source package root ahead of the caller's cwd before
# importing Avibe modules so a project named ``core`` in an agent workspace
# cannot shadow the server.
if __package__ in {None, ""}:
    _PACKAGE_ROOT = Path(__file__).resolve().parent.parent
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))

from config.atomic_io import write_atomic
from core.computer_use import (
    COMPUTER_USE_LEASE_FILE,
    COMPUTER_USE_LEASE_LOCK_FILE,
    COMPUTER_USE_TOOL_SNAPSHOT_SHA256,
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseState,
    ComputerUseStatus,
    computer_use_state_path,
    effective_computer_use_status,
    read_computer_use_state,
)

_LEASE_TTL_SECONDS = 60.0
_LEASE_HEARTBEAT_SECONDS = 10.0
_UPSTREAM_RESPONSE_LIMIT = 128 * 1024 * 1024

_INPUT_TOOLS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "hotkey",
        "set_value",
    }
)
_OBSERVE_TOOLS = frozenset({"get_window_state", "zoom"})
_ALLOWED_TARGET_KEYS = frozenset({"kind", "pid", "window_id"})
_COMMAND_MODIFIERS = frozenset({"cmd", "command", "meta"})
_SHIFT_MODIFIERS = frozenset({"shift"})


class ComputerServerError(RuntimeError):
    """A request cannot be forwarded safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class UpstreamUnavailable(RuntimeError):
    """The Cua proxy exited or rejected its transport."""


class Upstream(Protocol):
    serial: int

    @property
    def alive(self) -> bool: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]: ...

    async def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Lease:
    holder: str
    refreshed_at: float
    epoch: int
    daemon_instance_id: str
    daemon_generation: int

    @property
    def daemon_key(self) -> tuple[str, int]:
        return (self.daemon_instance_id, self.daemon_generation)


class DesktopLeaseManager:
    """Cross-process desktop lease protected by an operating-system lock."""

    def __init__(
        self,
        directory: Path,
        *,
        now: Callable[[], float] = time.monotonic,
        ttl_seconds: float = _LEASE_TTL_SECONDS,
    ) -> None:
        self._record_path = directory / COMPUTER_USE_LEASE_FILE
        self._lock_path = directory / COMPUTER_USE_LEASE_LOCK_FILE
        self._now = now
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _parse(payload: Any) -> Lease | None:
        if not isinstance(payload, dict):
            return None
        try:
            holder = payload["holder"]
            refreshed_at = payload["refreshed_at"]
            epoch = payload["epoch"]
            daemon_instance_id = payload["daemon_instance_id"]
            daemon_generation = payload["daemon_generation"]
        except KeyError:
            return None
        if (
            not isinstance(holder, str)
            or isinstance(refreshed_at, bool)
            or not isinstance(refreshed_at, (int, float))
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
            or not isinstance(daemon_instance_id, str)
            or not daemon_instance_id
            or isinstance(daemon_generation, bool)
            or not isinstance(daemon_generation, int)
            or daemon_generation < 0
        ):
            return None
        return Lease(
            holder=holder,
            refreshed_at=float(refreshed_at),
            epoch=epoch,
            daemon_instance_id=daemon_instance_id,
            daemon_generation=daemon_generation,
        )

    def _read(self) -> Lease | None:
        try:
            payload = json.loads(self._record_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return None
        return self._parse(payload)

    def _write(self, lease: Lease) -> None:
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(
            self._record_path,
            (
                json.dumps(
                    {
                        "holder": lease.holder,
                        "refreshed_at": lease.refreshed_at,
                        "epoch": lease.epoch,
                        "daemon_instance_id": lease.daemon_instance_id,
                        "daemon_generation": lease.daemon_generation,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )

    def _under_lock(self, operation: Callable[[], Any]) -> Any:
        if os.name == "nt":
            raise ComputerServerError(
                "platform_unsupported",
                "Computer use is available on macOS in Phase 1.",
            )
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                return operation()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, session: str, daemon_key: tuple[str, int]) -> Lease:
        def operation() -> Lease:
            previous = self._read()
            now = self._now()
            valid = (
                previous is not None
                and previous.daemon_key == daemon_key
                and now - previous.refreshed_at < self._ttl_seconds
            )
            if valid and previous.holder != session:
                if previous.holder:
                    raise ComputerServerError(
                        "desktop_busy",
                        f"Desktop computer use is held by session {previous.holder!r}.",
                    )
            if valid and previous.holder == session:
                epoch = previous.epoch
            else:
                epoch = (previous.epoch if previous is not None else 0) + 1
            lease = Lease(
                holder=session,
                refreshed_at=now,
                epoch=epoch,
                daemon_instance_id=daemon_key[0],
                daemon_generation=daemon_key[1],
            )
            self._write(lease)
            return lease

        return self._under_lock(operation)

    def refresh(self, lease: Lease) -> bool:
        def operation() -> bool:
            current = self._read()
            if (
                current is None
                or current.holder != lease.holder
                or current.epoch != lease.epoch
                or current.daemon_key != lease.daemon_key
            ):
                return False
            self._write(
                Lease(
                    holder=current.holder,
                    refreshed_at=self._now(),
                    epoch=current.epoch,
                    daemon_instance_id=current.daemon_instance_id,
                    daemon_generation=current.daemon_generation,
                )
            )
            return True

        return bool(self._under_lock(operation))

    def release(self, lease: Lease) -> None:
        def operation() -> None:
            current = self._read()
            if (
                current is None
                or current.holder != lease.holder
                or current.epoch != lease.epoch
                or current.daemon_key != lease.daemon_key
            ):
                return
            self._write(
                Lease(
                    holder="",
                    refreshed_at=self._now(),
                    epoch=current.epoch,
                    daemon_instance_id=current.daemon_instance_id,
                    daemon_generation=current.daemon_generation,
                )
            )

        self._under_lock(operation)


class JsonRpcUpstream:
    """One owned ``cua-driver mcp`` child with a multiplexed JSON-RPC pipe."""

    _serial_counter = 0

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        type(self)._serial_counter += 1
        self.serial = type(self)._serial_counter
        self._process = process
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(
            self._reader_loop(),
            name=f"computer-use-upstream-reader-{self.serial}",
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(),
            name=f"computer-use-upstream-stderr-{self.serial}",
        )

    @property
    def alive(self) -> bool:
        return self._process.returncode is None

    @classmethod
    async def start(cls, state: ComputerUseState) -> "JsonRpcUpstream":
        if (
            state.proxy_executable is None
            or state.socket_path is None
            or state.host_bundle_id is None
        ):
            raise UpstreamUnavailable("ready desktop state lacks proxy fields")
        env = os.environ.copy()
        env.update(
            {
                "CUA_DRIVER_EMBEDDED": "1",
                "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
                "CUA_DRIVER_RS_UPDATE_CHECK": "0",
            }
        )
        env.pop("CUA_DRIVER_WINDOW_CHANGE_TIMEOUT_MS", None)
        process = await asyncio.create_subprocess_exec(
            state.proxy_executable,
            "mcp",
            "--embedded",
            "--socket",
            state.socket_path,
            "--host-bundle-id",
            state.host_bundle_id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_UPSTREAM_RESPONSE_LIMIT,
        )
        upstream = cls(process)
        try:
            await upstream.request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "avibe-computer",
                        "version": "1",
                    },
                },
            )
            await upstream.notify("notifications/initialized", {})
        except BaseException:
            await upstream.close()
            raise
        return upstream

    async def _write(self, payload: Mapping[str, Any]) -> None:
        if self._process.stdin is None or not self.alive:
            raise UpstreamUnavailable("computer-use upstream is not running")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise UpstreamUnavailable("computer-use upstream pipe closed") from exc

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params),
            }
        )

    async def _reader_loop(self) -> None:
        stdout = self._process.stdout
        assert stdout is not None
        error: BaseException = UpstreamUnavailable("computer-use upstream exited")
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
        except BaseException as exc:
            error = exc
            raise
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _stderr_loop(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        while await stderr.readline():
            pass

    async def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None:
            stdin.close()
            with suppress(BrokenPipeError, ConnectionError):
                await stdin.wait_closed()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()
        for task in (self._reader_task, self._stderr_task):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@dataclass
class _SessionState:
    active_proxy_serial: int | None = None
    active_daemon_key: tuple[str, int] | None = None
    last_epoch: int | None = None
    observed_windows: set[tuple[int | None, int]] = field(default_factory=set)


def _load_snapshot(state_path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    state = read_computer_use_state(state_path)
    if state is None:
        raise RuntimeError("computer-use state is missing")
    try:
        snapshot_bytes = state.tool_snapshot.path.read_bytes()
    except OSError as exc:
        raise RuntimeError("computer-use tool snapshot is unreadable") from exc
    actual_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if (
        actual_sha256 != state.tool_snapshot.sha256
        or actual_sha256 != COMPUTER_USE_TOOL_SNAPSHOT_SHA256
    ):
        raise RuntimeError("computer-use tool snapshot digest is not approved")
    payload = json.loads(snapshot_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("computer-use tool snapshot has an unsupported schema")
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("computer-use tool snapshot has no tools array")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(COMPUTER_USE_TOOL_NAMES) or set(names) != set(
        COMPUTER_USE_TOOL_NAMES
    ):
        raise RuntimeError("computer-use tool snapshot does not match the approved surface")

    accepted_session: set[str] = set()
    advertised: list[dict[str, Any]] = []
    for source in tools:
        if not isinstance(source, dict):
            raise RuntimeError("computer-use tool snapshot contains a non-object tool")
        tool = deepcopy(source)
        name = str(tool["name"])
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RuntimeError(f"computer-use tool {name!r} has no object input schema")
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            schema["properties"] = properties
        if "session" in properties:
            accepted_session.add(name)
        properties["session"] = {
            "type": "string",
            "minLength": 1,
            "description": (
                "Required Avibe session id from the current conversation. "
                "Use the same value for every call in one task."
            ),
        }
        required = schema.get("required")
        if not isinstance(required, list):
            required = []
        schema["required"] = [*dict.fromkeys([*required, "session"])]
        tool.pop("outputSchema", None)
        advertised.append(tool)
    return advertised, accepted_session


def _error_result(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {"code": code, "message": message, **details}
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        ],
        "isError": True,
    }


def _fold_tool_result(message: dict[str, Any]) -> dict[str, Any]:
    if "error" in message:
        return _error_result(
            "upstream_error",
            "The computer-use driver rejected the request.",
            upstream=message["error"],
        )
    result = deepcopy(message.get("result"))
    if not isinstance(result, dict):
        return _error_result(
            "upstream_protocol_error",
            "The computer-use driver returned an invalid result.",
        )
    structured = result.pop("structuredContent", None)
    if structured is not None:
        content = result.get("content")
        if not isinstance(content, list):
            content = []
        content.append(
            {
                "type": "text",
                "text": "Structured content:\n"
                + json.dumps(
                    structured,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        result["content"] = content
    result.pop("outputSchema", None)
    return result


def _window_key(arguments: Mapping[str, Any]) -> tuple[int | None, int] | None:
    target = arguments.get("target")
    if isinstance(target, dict) and target.get("kind") == "window":
        pid = target.get("pid")
        window_id = target.get("window_id")
    else:
        pid = arguments.get("pid")
        window_id = arguments.get("window_id")
    if isinstance(pid, bool) or not isinstance(pid, int):
        pid = None
    if isinstance(window_id, bool) or not isinstance(window_id, int):
        return None
    return (pid, window_id)


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("⌘", "cmd")


def _focus_shortcut(arguments: Mapping[str, Any], tool_name: str) -> bool:
    keys: list[str] = []
    if tool_name == "hotkey":
        raw = arguments.get("keys")
        if isinstance(raw, list):
            keys = [_normalize_key(value) for value in raw]
        elif isinstance(raw, str):
            keys = [_normalize_key(value) for value in raw.replace("+", " ").split()]
    elif tool_name == "press_key":
        raw_modifiers = arguments.get("modifiers")
        if isinstance(raw_modifiers, list):
            keys.extend(_normalize_key(value) for value in raw_modifiers)
        keys.append(_normalize_key(arguments.get("key")))
    normalized = {key for key in keys if key}
    if not normalized.intersection(_COMMAND_MODIFIERS):
        return False
    non_modifiers = normalized - _COMMAND_MODIFIERS - _SHIFT_MODIFIERS
    if non_modifiers == {"l"}:
        return True
    if non_modifiers == {"g"} and normalized.intersection(_SHIFT_MODIFIERS):
        return True
    if len(non_modifiers) == 1 and next(iter(non_modifiers)) in set("123456789[]"):
        return True
    return False


def _validate_input(tool_name: str, arguments: Mapping[str, Any]) -> None:
    if tool_name not in _INPUT_TOOLS:
        return
    if arguments.get("delivery_mode") == "foreground":
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground input is disabled on macOS computer use.",
        )
    if "scope" in arguments and arguments.get("scope") != "window":
        raise ComputerServerError(
            "window_target_required",
            "Input must use the approved window location.",
        )
    element_token = arguments.get("element_token")
    has_element_token = isinstance(element_token, str) and bool(element_token.strip())
    target = arguments.get("target")
    if target is not None:
        if (
            not isinstance(target, dict)
            or set(target) - _ALLOWED_TARGET_KEYS
            or target.get("kind") != "window"
            or isinstance(target.get("pid"), bool)
            or not isinstance(target.get("pid"), int)
            or isinstance(target.get("window_id"), bool)
            or not isinstance(target.get("window_id"), int)
        ):
            raise ComputerServerError(
                "window_target_required",
                "The input target shape is not an approved exact window target.",
            )
    elif not has_element_token:
        pid = arguments.get("pid")
        window_id = arguments.get("window_id")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or isinstance(window_id, bool)
            or not isinstance(window_id, int)
        ):
            raise ComputerServerError(
                "window_target_required",
                "Input must provide pid and window_id, or an element token.",
            )
    if _focus_shortcut(arguments, tool_name):
        raise ComputerServerError(
            "focus_shortcut_forbidden",
            "This shortcut may activate application chrome. Use launch_app with urls instead.",
        )


class ComputerUseServer:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
        status_reader: Callable[[], ComputerUseStatus] | None = None,
        upstream_factory: Callable[[ComputerUseState], Awaitable[Upstream]] = JsonRpcUpstream.start,
        lease_manager: DesktopLeaseManager | None = None,
    ) -> None:
        self._state_path = state_path or computer_use_state_path()
        self._tools, self._upstream_accepts_session = _load_snapshot(self._state_path)
        self._status_reader = status_reader or (
            lambda: effective_computer_use_status(state_path=self._state_path)
        )
        self._upstream_factory = upstream_factory
        self._lease_manager = lease_manager or DesktopLeaseManager(
            self._state_path.parent
        )
        self._upstream: Upstream | None = None
        self._upstream_daemon_key: tuple[str, int] | None = None
        self._upstream_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _SessionState] = {}

    @property
    def tools(self) -> list[dict[str, Any]]:
        return deepcopy(self._tools)

    async def _ensure_upstream(self, state: ComputerUseState) -> Upstream:
        async with self._upstream_lock:
            daemon_key = state.daemon_key
            if (
                self._upstream is not None
                and self._upstream.alive
                and self._upstream_daemon_key == daemon_key
            ):
                return self._upstream
            previous = self._upstream
            self._upstream = None
            self._upstream_daemon_key = None
            if previous is not None:
                await previous.close()
            upstream = await self._upstream_factory(state)
            self._upstream = upstream
            self._upstream_daemon_key = daemon_key
            for session_state in self._sessions.values():
                session_state.active_proxy_serial = None
                session_state.active_daemon_key = None
                session_state.observed_windows.clear()
            return upstream

    async def _discard_upstream(self, upstream: Upstream) -> None:
        """Drop and close the exact transport that reported a failure."""

        async with self._upstream_lock:
            current = self._upstream is upstream
            if current:
                self._upstream = None
                self._upstream_daemon_key = None
                for session_state in self._sessions.values():
                    session_state.active_proxy_serial = None
                    session_state.active_daemon_key = None
                    session_state.observed_windows.clear()
        # Close even when another transport has already replaced the shared
        # slot. The failed process still belongs to this request and must not
        # remain alive merely because it lost the race to replacement.
        await upstream.close()

    async def _upstream_call(
        self,
        upstream: Upstream,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        response = await upstream.request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
        )
        return _fold_tool_result(response)

    async def _revive_session(
        self,
        upstream: Upstream,
        session: str,
        state: ComputerUseState,
        lease: Lease,
        session_state: _SessionState,
    ) -> None:
        active = (
            session_state.active_proxy_serial == upstream.serial
            and session_state.active_daemon_key == state.daemon_key
        )
        epoch_changed = (
            session_state.last_epoch is not None
            and session_state.last_epoch != lease.epoch
        )
        if active and epoch_changed:
            await self._upstream_call(
                upstream,
                "end_session",
                {"session": session},
            )
            active = False
        if not active:
            result = await self._upstream_call(
                upstream,
                "start_session",
                {"session": session},
            )
            if result.get("isError"):
                raise ComputerServerError(
                    "session_unavailable",
                    "The driver could not create the named computer-use session.",
                )
        if epoch_changed or session_state.last_epoch is None:
            session_state.observed_windows.clear()
        session_state.active_proxy_serial = upstream.serial
        session_state.active_daemon_key = state.daemon_key
        session_state.last_epoch = lease.epoch

    async def _heartbeat(self, lease: Lease) -> None:
        try:
            while True:
                await asyncio.sleep(_LEASE_HEARTBEAT_SECONDS)
                if not await asyncio.to_thread(self._lease_manager.refresh, lease):
                    return
        except asyncio.CancelledError:
            raise

    def _require_observation(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        session_state: _SessionState,
    ) -> None:
        element_token = arguments.get("element_token")
        if tool_name not in _INPUT_TOOLS or (
            isinstance(element_token, str) and bool(element_token.strip())
        ):
            return
        key = _window_key(arguments)
        if key is None:
            return
        wildcard = (None, key[1])
        if key not in session_state.observed_windows and wildcard not in session_state.observed_windows:
            raise ComputerServerError(
                "observe_first",
                "Observe this exact window with get_window_state or zoom before sending input.",
            )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        session = arguments.get("session")
        if not isinstance(session, str) or not session.strip():
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
            )
        session = session.strip()
        if name not in COMPUTER_USE_TOOL_NAMES:
            return _error_result("unknown_tool", f"Unknown computer-use tool: {name}")
        if not isinstance(arguments, dict):
            return _error_result("invalid_arguments", "Tool arguments must be an object.")

        lock = self._session_locks.setdefault(session, asyncio.Lock())
        async with lock:
            lease: Lease | None = None
            setup_complete = False
            upstream: Upstream | None = None
            try:
                _validate_input(name, arguments)
                status = await asyncio.to_thread(self._status_reader)
                if status.status != "ready" or status.state is None:
                    return _error_result(
                        "computer_use_unavailable",
                        "Computer use is not ready.",
                        status=status.status,
                        reason=status.reason,
                    )
                state = status.state
                lease = await asyncio.to_thread(
                    self._lease_manager.acquire,
                    session,
                    state.daemon_key,
                )
                upstream = await self._ensure_upstream(state)
                session_state = self._sessions.setdefault(session, _SessionState())
                await self._revive_session(
                    upstream,
                    session,
                    state,
                    lease,
                    session_state,
                )
                self._require_observation(name, arguments, session_state)
                setup_complete = True

                forwarded = dict(arguments)
                forwarded["session"] = session
                if name not in self._upstream_accepts_session:
                    forwarded.pop("session", None)
                heartbeat = asyncio.create_task(
                    self._heartbeat(lease),
                    name=f"computer-use-lease-heartbeat-{session}",
                )
                try:
                    if name == "start_session":
                        result = {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "session": session,
                                            "status": "started",
                                        },
                                        sort_keys=True,
                                    ),
                                }
                            ]
                        }
                    else:
                        result = await self._upstream_call(
                            upstream,
                            name,
                            forwarded,
                        )
                finally:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat

                if name in _OBSERVE_TOOLS and not result.get("isError"):
                    key = _window_key(arguments)
                    if key is not None:
                        session_state.observed_windows.add(key)
                if name == "end_session":
                    session_state.active_proxy_serial = None
                    session_state.active_daemon_key = None
                    session_state.observed_windows.clear()
                    await asyncio.to_thread(self._lease_manager.release, lease)
                return result
            except ComputerServerError as exc:
                return _error_result(exc.code, str(exc))
            except asyncio.CancelledError:
                raise
            except UpstreamUnavailable:
                if upstream is not None:
                    await self._discard_upstream(upstream)
                return _error_result(
                    "upstream_unavailable",
                    "The computer-use proxy exited. Observe again before retrying.",
                )
            except Exception as exc:
                return _error_result(
                    "computer_use_error",
                    f"Computer use failed: {type(exc).__name__}",
                )
            finally:
                if lease is not None and not setup_complete:
                    await asyncio.to_thread(self._lease_manager.release, lease)

    async def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            if request_id is None:
                return None
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        if method.startswith("notifications/"):
            return None
        if request_id is None:
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "avibe-computer", "version": "1"},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.tools},
            }
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict):
                params = {}
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                result = _error_result(
                    "invalid_arguments",
                    "tools/call requires a tool name and argument object.",
                )
            else:
                result = await self.call_tool(name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }

    async def close(self) -> None:
        async with self._upstream_lock:
            upstream = self._upstream
            self._upstream = None
            self._upstream_daemon_key = None
        if upstream is not None:
            await upstream.close()


async def _serve_stdio() -> None:
    server = ComputerUseServer()
    write_lock = asyncio.Lock()
    tasks: set[asyncio.Task[None]] = set()

    async def respond(message: Mapping[str, Any]) -> None:
        response = await server.handle(message)
        if response is None:
            return
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        async with write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                message = {"jsonrpc": "2.0", "id": None}
            if not isinstance(message, dict):
                message = {"jsonrpc": "2.0", "id": None}
            task = asyncio.create_task(respond(message))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await server.close()


def main() -> None:
    asyncio.run(_serve_stdio())


if __name__ == "__main__":
    main()
