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
import logging
import os
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable, Mapping, Protocol

# Managed MCP launches this file as an absolute script with Python isolated
# mode. Put the installed/source package root ahead of the caller's cwd before
# importing Avibe modules so a project named ``core`` in an agent workspace
# cannot shadow the server.
if __package__ in {None, ""}:
    _PACKAGE_ROOT = Path(__file__).resolve().parent.parent
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))

from core.agent_core.ai.errors import redact_provider_text
from core.computer_use import (
    COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS,
    ComputerUseState,
    ComputerUseStatus,
    computer_use_state_path,
    effective_computer_use_status,
    read_computer_use_state,
)
from core.computer_script_handles import (
    ComputerScriptHandleError,
    bind_handle_to_request,
    handle_validated_by_driver,
    parse_handle_descriptor,
    validate_handle_is_current,
)

_TARGET_LOCK_WAIT_SECONDS = 5.0
_LEGACY_LEASE_FILE = "computer-lease.json"
_LEGACY_LEASE_LOCK_FILE = "computer-lease.lock"
_UPSTREAM_RESPONSE_LIMIT = 128 * 1024 * 1024
# Cua permits individual accessibility calls to run for 120 seconds. Keep the
# transport deadline above that documented bound while still preventing a dead
# reader from holding a target lock forever.
_UPSTREAM_REQUEST_TIMEOUT_SECONDS = 180.0
_UPSTREAM_DETAIL_LIMIT = 2048
_UPSTREAM_DIAGNOSTIC_SCAN_LIMIT = 8192
_UPSTREAM_DIAGNOSTIC_LOG_LIMIT = 20
_ADMISSION_RETRY_LIMIT = 1
# Cua retires a named session after five idle minutes and then refuses every
# ordinary call for that label before dispatching it; only ``start_session``
# revives the name. The daemon gate reports it as ``tool_invocation_failed``
# and the in-tool admission as an unstructured refusal, both with this text.
_DRIVER_SESSION_ENDED_CODES = frozenset({"tool_invocation_failed", "upstream_tool_error"})
_DRIVER_SESSION_ENDED_DETAIL = "session has ended"
_SESSION_LIFECYCLE_TOOLS = frozenset(
    {"start_session", "end_session", "get_session", "list_sessions"}
)
_NO_SCRIPT_HANDLE = object()
_PROXY_PROCESS_ENV_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
)

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
# Window activation is an exact-window mutation, not input: it takes the same
# target lock but needs no prior observation of the window's contents.
_WINDOW_MUTATION_TOOLS = _INPUT_TOOLS | {"set_window_frame", "bring_to_front"}
_OBSERVE_TOOLS = frozenset({"get_window_state", "zoom"})
_ALLOWED_TARGET_KEYS = frozenset({"kind", "pid", "window_id"})

logger = logging.getLogger(__name__)


class ComputerServerError(RuntimeError):
    """A request cannot be forwarded safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class UpstreamUnavailable(RuntimeError):
    """The Cua proxy exited or rejected its transport."""


class _DaemonGenerationChanged(RuntimeError):
    """The desktop state changed before this call could be admitted."""


class _DriverSessionEnded(RuntimeError):
    """The driver refused a call because it had already retired the session."""


def _driver_session_ended(result: Mapping[str, Any]) -> bool:
    """Whether the driver refused this call, undispatched, as an ended session."""

    if result.get("isError") is not True:
        return False
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return False
    detail = structured.get("detail")
    return (
        structured.get("code") in _DRIVER_SESSION_ENDED_CODES
        and isinstance(detail, str)
        and detail.startswith(_DRIVER_SESSION_ENDED_DETAIL)
    )


class Upstream(Protocol):
    serial: int

    @property
    def alive(self) -> bool: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]: ...

    async def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


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
        return self._process.returncode is None and not self._reader_task.done()

    @classmethod
    async def start(cls, state: ComputerUseState) -> "JsonRpcUpstream":
        if (
            state.proxy_executable is None
            or state.socket_path is None
            or state.host_bundle_id is None
        ):
            raise UpstreamUnavailable("ready desktop state lacks proxy fields")
        env = {
            key: value
            for key in _PROXY_PROCESS_ENV_KEYS
            if (value := os.environ.get(key))
        }
        env.update(
            {
                "CUA_DRIVER_EMBEDDED": "1",
                "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
                "CUA_DRIVER_RS_UPDATE_CHECK": "0",
            }
        )
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
        if not self.alive:
            raise UpstreamUnavailable("computer-use upstream reader is not running")
        self._request_id += 1
        request_id = self._request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        async def round_trip() -> dict[str, Any]:
            # The reader can reach EOF between the first liveness check and
            # registration. Recheck after the future is visible so either the
            # reader completes it or this request fails promptly.
            if self._reader_task.done():
                raise UpstreamUnavailable(
                    "computer-use upstream reader exited before request registration"
                )
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            if self._reader_task.done() and not future.done():
                raise UpstreamUnavailable(
                    "computer-use upstream reader exited before request delivery"
                )
            return await future

        try:
            try:
                return await asyncio.wait_for(
                    round_trip(),
                    timeout=_UPSTREAM_REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamUnavailable(
                    "computer-use upstream request exceeded the 180 second limit"
                ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

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
        malformed_lines = 0
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    detail = _bounded_diagnostic(line)
                    malformed_lines += 1
                    if malformed_lines <= _UPSTREAM_DIAGNOSTIC_LOG_LIMIT:
                        logger.warning(
                            "computer-use upstream returned malformed JSON: %s",
                            detail or _bounded_diagnostic(str(exc)),
                        )
                    elif malformed_lines == _UPSTREAM_DIAGNOSTIC_LOG_LIMIT + 1:
                        logger.warning(
                            "computer-use upstream malformed JSON log limit reached; "
                            "further lines suppressed"
                        )
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            detail = _bounded_diagnostic(str(exc))
            logger.warning(
                "computer-use upstream reader failed (%s): %s",
                type(exc).__name__,
                detail or type(exc).__name__,
            )
            error = UpstreamUnavailable(
                "computer-use upstream reader failed"
                + (f": {detail}" if detail else "")
            )
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(error)

    async def _stderr_loop(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        logged_lines = 0
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    return
                detail = _bounded_diagnostic(line)
                if detail and logged_lines < _UPSTREAM_DIAGNOSTIC_LOG_LIMIT:
                    logger.warning("computer-use upstream stderr: %s", detail)
                    logged_lines += 1
                elif detail and logged_lines == _UPSTREAM_DIAGNOSTIC_LOG_LIMIT:
                    logger.warning(
                        "computer-use upstream stderr log limit reached; "
                        "further lines suppressed"
                    )
                    logged_lines += 1
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            detail = _bounded_diagnostic(str(exc))
            logger.warning(
                "computer-use upstream stderr reader failed: %s",
                detail or type(exc).__name__,
            )

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
    observed_windows: set[tuple[int | None, int]] = field(default_factory=set)
    element_token_windows: dict[str, tuple[int, int]] = field(default_factory=dict)


def _load_snapshot(
    state_path: Path,
) -> tuple[list[dict[str, Any]], set[str], frozenset[str]]:
    state = read_computer_use_state(state_path)
    if state is None:
        raise RuntimeError("computer-use state is missing")
    try:
        snapshot_bytes = state.tool_snapshot.path.read_bytes()
    except OSError as exc:
        raise RuntimeError("computer-use tool snapshot is unreadable") from exc
    actual_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    approved_names = COMPUTER_USE_APPROVED_TOOL_SNAPSHOTS.get(actual_sha256)
    if actual_sha256 != state.tool_snapshot.sha256 or approved_names is None:
        raise RuntimeError("computer-use tool snapshot digest is not approved")
    payload = json.loads(snapshot_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("computer-use tool snapshot has an unsupported schema")
    tools = payload.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("computer-use tool snapshot has no tools array")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(approved_names) or set(names) != approved_names:
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
    return advertised, accepted_session, approved_names


def _bounded_diagnostic(value: Any) -> str | None:
    """Return a single-line diagnostic without carrying arbitrary payloads."""

    if isinstance(value, bytes):
        text = value[:_UPSTREAM_DIAGNOSTIC_SCAN_LIMIT].decode(
            "utf-8",
            errors="replace",
        )
    elif isinstance(value, str):
        text = value[:_UPSTREAM_DIAGNOSTIC_SCAN_LIMIT]
    else:
        return None
    text = "".join(
        character
        if character in "\r\n" or character >= " " and character != "\x7f"
        else " "
        for character in text
    )
    text = redact_provider_text(text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > _UPSTREAM_DETAIL_LIMIT:
        return text[: _UPSTREAM_DETAIL_LIMIT - 3].rstrip() + "..."
    return text


def _diagnostic_candidate(value: Any) -> str | bytes | None:
    if isinstance(value, (str, bytes)) and value.strip():
        return value
    return None


def _diagnostic_from_mapping(value: Any) -> str | bytes | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("message", "reason", "detail", "error"):
        detail = _diagnostic_candidate(value.get(key))
        if detail:
            return detail
    return None


def _diagnostic_from_content(value: Any) -> str | bytes | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        detail = _diagnostic_candidate(item.get("text"))
        if detail:
            return detail
    return None


def _driver_error_params(value: Mapping[str, Any]) -> dict[str, Any]:
    structured_params = value.get("params")
    params = dict(structured_params) if isinstance(structured_params, dict) else {}
    for key in ("message", "reason", "detail", "error"):
        if key not in params:
            continue
        diagnostic = _bounded_diagnostic(params[key])
        if diagnostic is None:
            params.pop(key)
        else:
            params[key] = diagnostic
    exit_code = value.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        params.setdefault("exit_code", exit_code)
    return params


def _error_result(
    code: str,
    message: str | None = None,
    *,
    detail: str | bytes | None = None,
    dispatched: bool | None = None,
    result_params: Mapping[str, Any] | None = None,
    **params: Any,
) -> dict[str, Any]:
    if dispatched is not None and dispatched is not False:
        raise ValueError("computer-use errors may only declare dispatched=false")
    payload_params = dict(result_params) if result_params is not None else {}
    payload_params.update(params)
    payload = {"code": code, "params": payload_params}
    if dispatched is False:
        payload["dispatched"] = False
    bounded_detail = _bounded_diagnostic(detail)
    if bounded_detail:
        payload["detail"] = bounded_detail
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        ],
        "structuredContent": payload,
        "isError": True,
    }


def _not_dispatched_error_result(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        return _error_result("computer_use_error", dispatched=False)
    code = structured.get("code")
    return _error_result(
        code if isinstance(code, str) else "computer_use_error",
        detail=structured.get("detail"),
        dispatched=False,
        result_params=(
            structured.get("params")
            if isinstance(structured.get("params"), Mapping)
            else None
        ),
    )


def _fold_tool_result(
    message: dict[str, Any],
    *,
    tool_name: str | None = None,
) -> dict[str, Any]:
    if "error" in message:
        upstream_error = message.get("error")
        detail = _diagnostic_from_mapping(upstream_error)
        params: dict[str, Any] = {}
        if isinstance(upstream_error, Mapping):
            rpc_code = upstream_error.get("code")
            if isinstance(rpc_code, (int, str)) and not isinstance(rpc_code, bool):
                params["rpc_code"] = rpc_code
            if detail is None:
                detail = _diagnostic_from_mapping(upstream_error.get("data"))
        return _error_result(
            "upstream_error",
            detail=detail,
            result_params=params,
        )
    result = deepcopy(message.get("result"))
    if not isinstance(result, dict):
        return _error_result("upstream_protocol_error")
    structured = result.pop("structuredContent", None)
    if result.get("isError") is True:
        if isinstance(structured, dict) and isinstance(structured.get("code"), str):
            code = structured["code"]
            detail = (
                _diagnostic_from_mapping(structured)
                or _diagnostic_from_mapping(structured.get("params"))
                or _diagnostic_from_content(result.get("content"))
            )
            return _error_result(
                code,
                detail=detail,
                dispatched=False if structured.get("dispatched") is False else None,
                result_params=_driver_error_params(structured),
            )
        return _error_result(
            "upstream_tool_error",
            detail=_diagnostic_from_content(result.get("content")),
            tool=tool_name or "",
        )
    if structured is not None:
        if isinstance(structured, dict):
            result["structuredContent"] = structured
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


def _target_lock_keys(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    element_token_windows: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[tuple[str, str], ...]:
    if tool_name in _WINDOW_MUTATION_TOOLS:
        key = _window_key(arguments)
        if key is None or key[0] is None:
            element_token = arguments.get("element_token")
            if (
                isinstance(element_token, str)
                and element_token.strip()
                and element_token_windows is not None
            ):
                key = element_token_windows.get(element_token.strip())
        if key is None or key[0] is None:
            raise ComputerServerError(
                "window_target_required",
                "The operation must target an exact window with pid and window_id.",
            )
        pid, window_id = key
        return (("window", f"{pid}:{window_id}"),)
    if tool_name == "launch_app":
        identity = arguments.get("bundle_id") or arguments.get("name")
        if not isinstance(identity, str) or not identity.strip():
            raise ComputerServerError(
                "invalid_arguments",
                "launch_app must provide a bundle_id or name.",
            )
        return (("launch", identity.strip()),)
    if tool_name == "kill_app":
        pid = arguments.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ComputerServerError(
                "invalid_arguments",
                "kill_app must provide a positive pid.",
            )
        return (("pid", str(pid)),)
    if tool_name == "clipboard_write":
        return (("clipboard", "clipboard"),)
    return ()


class _TargetLockLease:
    def __init__(
        self,
        manager: "_TargetLockManager",
        keys: tuple[tuple[str, str], ...],
    ) -> None:
        self._manager = manager
        self._keys = keys
        self._released = False

    async def __aenter__(self) -> "_TargetLockLease":
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        if not self._released:
            self._released = True
            await self._manager.release(self._keys)


class _TargetLockManager:
    """In-process locks for one Controller's independently usable targets."""

    def __init__(self, *, wait_seconds: float = _TARGET_LOCK_WAIT_SECONDS) -> None:
        self._wait_seconds = wait_seconds
        self._condition = asyncio.Condition()
        self._held: dict[tuple[str, str], int] = {}

    @staticmethod
    def _conflicts(
        held_key: tuple[str, str],
        requested_key: tuple[str, str],
    ) -> bool:
        if held_key == requested_key:
            return True
        held_kind, held_value = held_key
        requested_kind, requested_value = requested_key
        if held_kind == "pid" and requested_kind == "window":
            return requested_value.startswith(f"{held_value}:")
        if held_kind == "window" and requested_kind == "pid":
            return held_value.startswith(f"{requested_value}:")
        return False

    def _available(self, keys: tuple[tuple[str, str], ...]) -> bool:
        return not any(
            self._conflicts(held_key, requested_key)
            for held_key in self._held
            for requested_key in keys
        )

    async def acquire(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        element_token_windows: Mapping[str, tuple[int, int]] | None = None,
    ) -> _TargetLockLease:
        keys = _target_lock_keys(
            tool_name,
            arguments,
            element_token_windows=element_token_windows,
        )
        if not keys:
            return _TargetLockLease(self, ())
        deadline = asyncio.get_running_loop().time() + self._wait_seconds
        async with self._condition:
            while not self._available(keys):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ComputerServerError(
                        "target_busy",
                        "The requested Computer Use target is busy.",
                    )
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except asyncio.TimeoutError as exc:
                    raise ComputerServerError(
                        "target_busy",
                        "The requested Computer Use target is busy.",
                    ) from exc
            for key in keys:
                self._held[key] = self._held.get(key, 0) + 1
        return _TargetLockLease(self, keys)

    async def release(self, keys: tuple[tuple[str, str], ...]) -> None:
        if not keys:
            return
        async with self._condition:
            for key in keys:
                count = self._held.get(key, 0)
                if count <= 1:
                    self._held.pop(key, None)
                else:
                    self._held[key] = count - 1
            self._condition.notify_all()


def _remove_legacy_lease_files(directory: Path) -> None:
    for filename in (_LEGACY_LEASE_FILE, _LEGACY_LEASE_LOCK_FILE):
        try:
            (directory / filename).unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug(
                "could not remove legacy Computer Use file %s",
                directory / filename,
                exc_info=True,
            )


def _validate_input(tool_name: str, arguments: Mapping[str, Any]) -> None:
    if "click_mode" in arguments and tool_name != "click":
        raise ComputerServerError(
            "invalid_arguments",
            "click_mode is valid only for the click tool.",
        )
    if tool_name not in _INPUT_TOOLS:
        return
    if "click_mode" in arguments:
        click_mode = arguments.get("click_mode")
        if not isinstance(click_mode, str) or click_mode not in {"auto", "raw"}:
            raise ComputerServerError(
                "invalid_arguments",
                'click_mode must be "auto" or "raw".',
            )
        if click_mode == "raw":
            pid = arguments.get("pid")
            window_id = arguments.get("window_id")
            x = arguments.get("x")
            y = arguments.get("y")
            count = arguments.get("count", 1)
            modifiers = arguments.get("modifier", [])
            if (
                isinstance(pid, bool)
                or not isinstance(pid, int)
                or isinstance(window_id, bool)
                or not isinstance(window_id, int)
                or isinstance(x, bool)
                or not isinstance(x, (int, float))
                or isinstance(y, bool)
                or not isinstance(y, (int, float))
                or arguments.get("element_token") is not None
                or arguments.get("target") is not None
                or arguments.get("action", "press") not in {"press", "click"}
                or arguments.get("button", "left") != "left"
                or arguments.get("delivery_mode", "background") != "background"
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count != 1
                or not isinstance(modifiers, list)
                or modifiers
            ):
                raise ComputerServerError(
                    "invalid_arguments",
                    'click_mode="raw" requires an exact pid + window_id '
                    "background left-click with x/y, count=1, and no modifiers.",
                )
    # ``delivery_mode`` passes through: the driver owns the background default
    # and the bounded foreground rung, and a change of focus is not a refusal.
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


class ComputerUseServer:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
        status_reader: Callable[[], ComputerUseStatus] | None = None,
        upstream_factory: Callable[[ComputerUseState], Awaitable[Upstream]] = JsonRpcUpstream.start,
        target_lock_manager: _TargetLockManager | None = None,
    ) -> None:
        self._state_path = state_path or computer_use_state_path()
        _remove_legacy_lease_files(self._state_path.parent)
        (
            self._tools,
            self._upstream_accepts_session,
            self._tool_names,
        ) = _load_snapshot(self._state_path)
        self._status_reader = status_reader or (
            lambda: effective_computer_use_status(state_path=self._state_path)
        )
        self._upstream_factory = upstream_factory
        self._target_lock_manager = target_lock_manager or _TargetLockManager()
        self._upstream: Upstream | None = None
        self._upstream_daemon_key: tuple[str, int] | None = None
        self._upstream_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, _SessionState] = {}

    @property
    def tools(self) -> list[dict[str, Any]]:
        return deepcopy(self._tools)

    def _current_ready_daemon_key(self) -> tuple[str, int] | None:
        status = self._status_reader()
        if status.status != "ready" or status.state is None:
            return None
        return status.state.daemon_key

    def _revalidate_daemon_key(self, expected: tuple[str, int]) -> None:
        if self._current_ready_daemon_key() != expected:
            raise _DaemonGenerationChanged(
                "computer-use daemon generation changed before forwarding"
            )

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
                session_state.element_token_windows.clear()
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
                    session_state.element_token_windows.clear()
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
        return _fold_tool_result(response, tool_name=name)

    @staticmethod
    def _commit_session_ended(session_state: _SessionState) -> None:
        session_state.active_proxy_serial = None
        session_state.active_daemon_key = None
        session_state.observed_windows.clear()
        session_state.element_token_windows.clear()

    @staticmethod
    def _commit_session_started(
        session_state: _SessionState,
        upstream: Upstream,
        state: ComputerUseState,
    ) -> None:
        session_state.active_proxy_serial = upstream.serial
        session_state.active_daemon_key = state.daemon_key

    async def _prepare_session(
        self,
        upstream: Upstream,
        session: str,
        state: ComputerUseState,
        session_state: _SessionState,
        *,
        explicit_start: bool,
    ) -> None:
        active = (
            session_state.active_proxy_serial == upstream.serial
            and session_state.active_daemon_key == state.daemon_key
        )
        if not active and not explicit_start:
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
            self._commit_session_started(
                session_state,
                upstream,
                state,
            )

    def _require_observation(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        session_state: _SessionState,
    ) -> None:
        if tool_name not in _INPUT_TOOLS:
            return
        element_token = arguments.get("element_token")
        if isinstance(element_token, str) and element_token.strip():
            if element_token.strip() not in session_state.element_token_windows:
                raise ComputerServerError(
                    "observe_first",
                    "Observe this exact window with get_window_state or zoom before sending input.",
                )
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

    @staticmethod
    def _element_tokens(value: Any) -> set[str]:
        tokens: set[str] = set()

        def visit(current: Any) -> None:
            if isinstance(current, Mapping):
                token = current.get("element_token")
                if isinstance(token, str) and token.strip():
                    tokens.add(token.strip())
                for nested in current.values():
                    visit(nested)
            elif isinstance(current, list):
                for nested in current:
                    visit(nested)

        visit(value)
        return tokens

    def _record_observation(
        self,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        session_state: _SessionState,
    ) -> None:
        key = _window_key(arguments)
        if key is not None:
            session_state.observed_windows.add(key)
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            return

        observed_pid = structured.get("pid")
        observed_window_id = structured.get("window_id")
        if (
            isinstance(observed_pid, int)
            and not isinstance(observed_pid, bool)
            and isinstance(observed_window_id, int)
            and not isinstance(observed_window_id, bool)
        ):
            exact_key = (observed_pid, observed_window_id)
        elif key is not None and key[0] is not None:
            exact_key = key
        else:
            exact_key = None
        if exact_key is not None:
            for token in self._element_tokens(structured):
                session_state.element_token_windows[token] = exact_key

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        handle_descriptor: Any = _NO_SCRIPT_HANDLE,
    ) -> dict[str, Any]:
        session = arguments.get("session")
        if not isinstance(session, str) or not session.strip():
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
                dispatched=False,
            )
        session = session.strip()
        if name not in self._tool_names:
            return _error_result("unknown_tool", dispatched=False, tool=name)
        if not isinstance(arguments, dict):
            return _error_result(
                "invalid_arguments",
                "Tool arguments must be an object.",
                dispatched=False,
            )
        parsed_handle = None
        if handle_descriptor is not _NO_SCRIPT_HANDLE:
            try:
                parsed_handle = parse_handle_descriptor(handle_descriptor)
                bind_handle_to_request(parsed_handle, name, arguments)
            except ComputerScriptHandleError as exc:
                return _error_result(
                    exc.code,
                    dispatched=False,
                    result_params=exc.params,
                )

        lock = self._session_locks.setdefault(session, asyncio.Lock())
        async with lock:
            try:
                _validate_input(name, arguments)
            except ComputerServerError as exc:
                return _error_result(exc.code, str(exc), dispatched=False)

            admission_attempt = 0
            # Revive a driver-retired session at most once per call. A second
            # refusal right after a successful start is returned as-is.
            revival_available = name not in _SESSION_LIFECYCLE_TOOLS
            while True:
                upstream: Upstream | None = None
                session_state: _SessionState | None = None
                pre_forward = True
                driver_validates_handle = False
                try:
                    status = await asyncio.to_thread(self._status_reader)
                    if status.status != "ready" or status.state is None:
                        return _error_result(
                            "computer_use_unavailable",
                            "Computer use is not ready.",
                            dispatched=False,
                            status=status.status,
                            reason=status.reason,
                        )
                    state = status.state
                    upstream = await self._ensure_upstream(state)
                    await asyncio.to_thread(
                        self._revalidate_daemon_key,
                        state.daemon_key,
                    )
                    session_state = self._sessions.setdefault(
                        session,
                        _SessionState(),
                    )
                    await self._prepare_session(
                        upstream,
                        session,
                        state,
                        session_state,
                        explicit_start=name == "start_session",
                    )
                    if handle_descriptor is not _NO_SCRIPT_HANDLE:
                        async def discover(
                            discovery_tool: str,
                            discovery_arguments: Mapping[str, Any],
                        ) -> dict[str, Any]:
                            forwarded_discovery = {
                                **discovery_arguments,
                                "session": session,
                            }
                            if discovery_tool not in self._upstream_accepts_session:
                                forwarded_discovery.pop("session", None)
                            discovered = await self._upstream_call(
                                upstream,
                                discovery_tool,
                                forwarded_discovery,
                            )
                            if revival_available and _driver_session_ended(discovered):
                                raise _DriverSessionEnded(discovery_tool)
                            return discovered

                        async def check_handle() -> dict[str, Any] | None:
                            try:
                                validation_error = await validate_handle_is_current(
                                    parsed_handle,
                                    name,
                                    discover,
                                )
                            except ComputerScriptHandleError as exc:
                                return _error_result(
                                    exc.code,
                                    dispatched=False,
                                    result_params=exc.params,
                                )
                            if validation_error is not None:
                                return _not_dispatched_error_result(validation_error)
                            return None

                        driver_validates_handle = handle_validated_by_driver(
                            parsed_handle,
                            name,
                        )
                        if not driver_validates_handle:
                            refusal = await check_handle()
                            if refusal is not None:
                                return refusal
                    self._require_observation(name, arguments, session_state)
                    forwarded = dict(arguments)
                    forwarded["session"] = session
                    if name not in self._upstream_accepts_session:
                        forwarded.pop("session", None)

                    target_lock = await self._target_lock_manager.acquire(
                        name,
                        arguments,
                        element_token_windows=session_state.element_token_windows,
                    )
                    async with target_lock:
                        await asyncio.to_thread(
                            self._revalidate_daemon_key,
                            state.daemon_key,
                        )
                        pre_forward = False
                        result = await self._upstream_call(
                            upstream,
                            name,
                            forwarded,
                        )
                        if _driver_session_ended(result):
                            if revival_available:
                                raise _DriverSessionEnded(name)
                            # Still refused after a fresh start: the driver
                            # ran nothing, so say so instead of leaving the
                            # outcome ambiguous.
                            return _not_dispatched_error_result(result)

                    if driver_validates_handle and result.get("isError") is True:
                        # A failed read-only observation changed nothing. When
                        # the handle no longer names a live window, report it
                        # exactly as the pre-forward check would; any other
                        # failure stays as the driver returned it.
                        refusal = await check_handle()
                        if (
                            refusal is not None
                            and refusal["structuredContent"]["code"]
                            == "stale_window_handle"
                        ):
                            return refusal
                    if name in _OBSERVE_TOOLS and not result.get("isError"):
                        self._record_observation(arguments, result, session_state)
                    if name == "start_session":
                        if not result.get("isError"):
                            self._commit_session_started(
                                session_state,
                                upstream,
                                state,
                            )
                    elif name == "end_session" and not result.get("isError"):
                        self._commit_session_ended(session_state)
                        self._sessions.pop(session, None)
                    return result
                except _DriverSessionEnded:
                    # The driver ran nothing. Its element tokens and snapshots
                    # for the ended episode are retired, so drop the matching
                    # Controller observations and start the name again.
                    if session_state is not None:
                        self._commit_session_ended(session_state)
                    revival_available = False
                    logger.info(
                        "computer-use driver retired session %s; starting it again",
                        session,
                    )
                    continue
                except _DaemonGenerationChanged:
                    if upstream is not None:
                        await self._discard_upstream(upstream)
                    if admission_attempt < _ADMISSION_RETRY_LIMIT:
                        admission_attempt += 1
                        continue
                    return _error_result(
                        "computer_use_unavailable",
                        "Computer use changed while the request was being admitted.",
                        dispatched=False,
                        status="unavailable",
                        reason="daemon_generation_changed",
                    )
                except ComputerServerError as exc:
                    return _error_result(
                        exc.code,
                        str(exc),
                        dispatched=False if pre_forward else None,
                    )
                except asyncio.CancelledError:
                    raise
                except UpstreamUnavailable as exc:
                    if upstream is not None:
                        await self._discard_upstream(upstream)
                    detail = _bounded_diagnostic(str(exc))
                    logger.warning(
                        "computer-use upstream unavailable during %s: %s",
                        name,
                        detail or type(exc).__name__,
                    )
                    return _error_result(
                        "upstream_unavailable",
                        "The computer-use proxy exited. Observe again before retrying.",
                        detail=detail,
                        dispatched=False if pre_forward else None,
                    )
                except Exception as exc:
                    detail = _bounded_diagnostic(str(exc))
                    logger.warning(
                        "computer-use call failed during %s (%s): %s",
                        name,
                        type(exc).__name__,
                        detail or type(exc).__name__,
                    )
                    return _error_result(
                        "computer_use_error",
                        detail=detail,
                        dispatched=False if pre_forward else None,
                        error_type=type(exc).__name__,
                    )

    async def end_session_if_active(self, session: str) -> dict[str, Any]:
        """Retire a session created by an earlier call without creating one."""

        normalized = str(session or "").strip()
        if not normalized:
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
            )
        session_state = self._sessions.get(normalized)
        if session_state is None or session_state.active_proxy_serial is None:
            return {"isError": False, "content": []}
        return await self.call_tool("end_session", {"session": normalized})

    async def retire_session(self, session: str) -> dict[str, Any]:
        """Release all Controller-owned Computer Use state for an archived Session."""

        normalized = str(session or "").strip()
        if not normalized:
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
            )

        expected_lock = self._session_locks.get(normalized)
        expected_state = self._sessions.get(normalized)
        result = await self.end_session_if_active(normalized)
        if expected_lock is None:
            return result

        async with expected_lock:
            # Archive closes Computer Use admission before this boundary, but
            # compare exact identities so a late cleanup cannot remove a newer
            # session generation or its lock.
            if self._session_locks.get(normalized) is not expected_lock:
                return result
            current_state = self._sessions.get(normalized)
            if (
                expected_state is not None
                and current_state is not None
                and current_state is not expected_state
            ):
                return result
            if current_state is not None:
                # When no state existed at the archive boundary, this is the
                # state created by a call that was admitted before the fence.
                self._sessions.pop(normalized, None)
            # ``end_session`` may already have removed the expected state.
            self._session_locks.pop(normalized, None)
        return result

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
