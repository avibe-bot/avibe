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
import math
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
from core.agent_core.ai.errors import redact_provider_text
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
from core.computer_script_handles import (
    ComputerScriptHandleError,
    bind_handle_to_request,
    parse_handle_descriptor,
    validate_handle_is_current,
)
from vibe.runtime import process_start_token

_LEASE_TTL_SECONDS = 60.0
_LEASE_HEARTBEAT_SECONDS = 10.0
_UPSTREAM_RESPONSE_LIMIT = 128 * 1024 * 1024
# Cua permits individual accessibility calls to run for 120 seconds. Keep the
# transport deadline above that documented bound while still preventing a dead
# reader from holding the desktop lease forever.
_UPSTREAM_REQUEST_TIMEOUT_SECONDS = 180.0
_UPSTREAM_DETAIL_LIMIT = 2048
_UPSTREAM_DIAGNOSTIC_SCAN_LIMIT = 8192
_UPSTREAM_DIAGNOSTIC_LOG_LIMIT = 20
_ADMISSION_RETRY_LIMIT = 1
_NO_SCRIPT_HANDLE = object()
_FOREGROUND_CONTROL_SOCKET = "foreground-control.sock"
_SOCKET_FLAVOR_HEX_BYTES = 12
_FOREGROUND_POINTER_TOLERANCE = 0.01
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
_OBSERVE_TOOLS = frozenset({"get_window_state", "zoom"})
_ALLOWED_TARGET_KEYS = frozenset({"kind", "pid", "window_id"})
_UPSTREAM_NOT_DISPATCHED_CODES = frozenset({"foreground_consent_required"})
_COMMAND_MODIFIERS = frozenset({"cmd", "command", "meta"})
_SHIFT_MODIFIERS = frozenset({"shift"})

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


class _LeaseLost(RuntimeError):
    """The caller no longer owns the desktop lease."""


class Upstream(Protocol):
    serial: int

    @property
    def alive(self) -> bool: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]: ...

    async def notify(self, method: str, params: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


class ForegroundControl(Protocol):
    async def request(
        self,
        operation: str,
        *,
        session: str,
        title: str,
        target: Mapping[str, Any] | None = None,
        restoration: Mapping[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]: ...


class UnixForegroundControl:
    """Client for the shell-owned, ephemeral foreground control socket."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path

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
        payload: dict[str, Any] = {
            "operation": operation,
            "session": session,
            "title": title,
        }
        if target is not None:
            payload["target"] = dict(target)
        if restoration is not None:
            payload["restoration"] = dict(restoration)
        if reason is not None:
            payload["reason"] = reason
        reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
        try:
            writer.write(
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
            )
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
        if not line:
            raise ConnectionError("foreground shell closed its control connection")
        response = json.loads(line.decode("utf-8"))
        if not isinstance(response, dict):
            raise ValueError("foreground shell returned a non-object response")
        return response


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


@dataclass(frozen=True)
class LeaseAcquisition:
    lease: Lease
    newly_claimed: bool


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

    def acquire(
        self,
        session: str,
        daemon_key: tuple[str, int],
        current_daemon_key: Callable[[], tuple[str, int] | None],
    ) -> LeaseAcquisition:
        def operation() -> LeaseAcquisition:
            if current_daemon_key() != daemon_key:
                raise _DaemonGenerationChanged(
                    "computer-use daemon generation changed during lease admission"
                )
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
                        "Desktop computer use is held by another Avibe Session.",
                    )
            if valid and previous.holder == session:
                epoch = previous.epoch
            else:
                epoch = (previous.epoch if previous is not None else 0) + 1
            newly_claimed = not (
                valid and previous is not None and previous.holder == session
            )
            lease = Lease(
                holder=session,
                refreshed_at=now,
                epoch=epoch,
                daemon_instance_id=daemon_key[0],
                daemon_generation=daemon_key[1],
            )
            self._write(lease)
            return LeaseAcquisition(
                lease=lease,
                newly_claimed=newly_claimed,
            )

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

    def release_holder(self, holder: str) -> None:
        """Release a current holder during explicit Avibe Session retirement."""

        normalized = str(holder or "").strip()
        if not normalized:
            return

        def operation() -> None:
            current = self._read()
            if current is None or current.holder != normalized:
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
    last_epoch: int | None = None
    observed_windows: set[tuple[int | None, int]] = field(default_factory=set)
    window_observations: dict[tuple[int, int], "_WindowObservation"] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class _WindowObservation:
    identity: "_WindowIdentity"
    snapshot_id: str | None
    capture_id: str | None
    element_tokens: frozenset[str]


@dataclass(frozen=True)
class _WindowIdentity:
    pid: int
    process_start_stamp: str
    window_id: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_start_stamp": self.process_start_stamp,
            "window_id": self.window_id,
        }


@dataclass(frozen=True)
class _ForegroundContract:
    target_identity: _WindowIdentity
    target_label: dict[str, str]
    prior_frontmost: _WindowIdentity
    pointer: dict[str, float]
    active_space_id: int

    @property
    def target(self) -> dict[str, Any]:
        return {**self.target_identity.as_dict(), **self.target_label}


@dataclass
class _RestorationComponent:
    recorded: Any
    attempted: bool = False
    verified: bool = False
    failure_detail: str | None = None

    def fail(self, detail: Any) -> None:
        bounded = _bounded_diagnostic(detail) or "restoration failed without detail"
        if self.failure_detail is None:
            self.failure_detail = bounded
        elif bounded not in self.failure_detail:
            self.failure_detail = _bounded_diagnostic(
                f"{self.failure_detail}; {bounded}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded": deepcopy(self.recorded),
            "attempted": self.attempted,
            "verified": self.verified,
            "failure_detail": self.failure_detail,
        }


@dataclass
class _ForegroundRestoration:
    prior_frontmost: _RestorationComponent
    pointer: _RestorationComponent
    active_space: _RestorationComponent

    @classmethod
    def from_contract(
        cls,
        contract: _ForegroundContract,
    ) -> "_ForegroundRestoration":
        return cls(
            prior_frontmost=_RestorationComponent(
                recorded=contract.prior_frontmost.as_dict()
            ),
            pointer=_RestorationComponent(recorded=deepcopy(contract.pointer)),
            active_space=_RestorationComponent(
                recorded=contract.active_space_id
            ),
        )

    def items(self) -> tuple[tuple[str, _RestorationComponent], ...]:
        return (
            ("prior_frontmost", self.prior_frontmost),
            ("pointer", self.pointer),
            ("active_space", self.active_space),
        )

    @property
    def restoration_ok(self) -> bool:
        return all(
            component.attempted
            and component.verified
            and component.failure_detail is None
            for _name, component in self.items()
        )

    def fail_all(self, detail: Any) -> None:
        for _name, component in self.items():
            component.fail(detail)

    def apply_native_result(self, response: Mapping[str, Any]) -> None:
        if response.get("ok") is not True:
            self.fail_all(
                response.get("error") or "native restoration failed"
            )
            return
        native = response.get("restoration")
        if not isinstance(native, Mapping):
            self.fail_all("native restoration result was missing")
            return
        components = native.get("components")
        if not isinstance(components, Mapping):
            self.fail_all("native restoration components were missing")
            return
        for name, component in self.items():
            native_component = components.get(name)
            if not isinstance(native_component, Mapping):
                component.fail(f"native {name} restoration result was missing")
                continue
            if native_component.get("recorded") != component.recorded:
                component.fail(
                    f"native {name} restoration changed the recorded value"
                )
            component.attempted = native_component.get("attempted") is True
            if not component.attempted:
                component.fail(f"native {name} restoration was not attempted")
            detail = native_component.get("failure_detail")
            if isinstance(detail, str) and detail.strip():
                component.fail(detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "components": {
                name: component.as_dict()
                for name, component in self.items()
            },
            "restoration_ok": self.restoration_ok,
        }


def _structured_content(result: Mapping[str, Any]) -> dict[str, Any] | None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return deepcopy(structured)
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        if text.startswith("Structured content:\n"):
            text = text.split("\n", 1)[1]
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _frontmost_window(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return None
    candidates = [
        window
        for window in windows
        if isinstance(window, Mapping)
        and window.get("on_current_space") is True
        and window.get("is_on_screen") is True
        and window.get("minimized") is not True
        and isinstance(window.get("z_index"), int)
        and not isinstance(window.get("z_index"), bool)
        and isinstance(window.get("pid"), int)
        and not isinstance(window.get("pid"), bool)
        and isinstance(window.get("window_id"), int)
        and not isinstance(window.get("window_id"), bool)
    ]
    if not candidates:
        return None
    highest = max(int(window["z_index"]) for window in candidates)
    winners = [window for window in candidates if window["z_index"] == highest]
    if len(winners) != 1:
        return None
    window = winners[0]
    return {
        "pid": int(window["pid"]),
        "window_id": int(window["window_id"]),
        "app_name": str(window.get("app_name") or ""),
        "title": str(window.get("title") or ""),
    }


def _window_key_from_record(window: Mapping[str, Any]) -> tuple[int, int] | None:
    pid = window.get("pid")
    window_id = window.get("window_id")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(window_id, bool)
        or not isinstance(window_id, int)
    ):
        return None
    return (pid, window_id)


def _process_start_stamp(pid: int) -> str | None:
    token = process_start_token(pid)
    if not isinstance(token, str):
        return None
    try:
        started_at = float(token)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(started_at) or started_at <= 0:
        return None
    return str(round(started_at * 1_000_000))


def _record_window_identity(
    window: Mapping[str, Any],
    process_start_stamp_reader: Callable[[int], str | None],
) -> _WindowIdentity | None:
    key = _window_key_from_record(window)
    if key is None:
        return None
    process_stamp = process_start_stamp_reader(key[0])
    if not isinstance(process_stamp, str) or not process_stamp:
        return None
    return _WindowIdentity(
        pid=key[0],
        process_start_stamp=process_stamp,
        window_id=key[1],
    )


def _verify_window_identity(
    identity: _WindowIdentity,
    window: Mapping[str, Any],
    process_start_stamp_reader: Callable[[int], str | None],
) -> bool:
    return (
        _window_key_from_record(window) == (identity.pid, identity.window_id)
        and process_start_stamp_reader(identity.pid)
        == identity.process_start_stamp
    )


def _window_by_key(
    payload: Mapping[str, Any],
    key: tuple[int | None, int],
) -> dict[str, Any] | None:
    windows = payload.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if (
            isinstance(window, Mapping)
            and window.get("pid") == key[0]
            and window.get("window_id") == key[1]
        ):
            return dict(window)
    return None


def _window_by_identity(
    payload: Mapping[str, Any],
    identity: _WindowIdentity,
    process_start_stamp_reader: Callable[[int], str | None],
) -> dict[str, Any] | None:
    window = _window_by_key(payload, (identity.pid, identity.window_id))
    if window is None or not _verify_window_identity(
        identity,
        window,
        process_start_stamp_reader,
    ):
        return None
    return window


def _float_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _foreground_error(
    code: str,
    message: str,
    *,
    outcome: str = "unattempted",
    dispatched: bool | None = None,
    **params: Any,
) -> dict[str, Any]:
    params = {"foreground_outcome": outcome, **params}
    return _error_result(
        code,
        message,
        dispatched=dispatched,
        result_params=params,
    )


def _with_foreground_outcome(
    result: dict[str, Any],
    *,
    outcome: str,
    restoration: Mapping[str, Any],
    effect: str | None = None,
) -> dict[str, Any]:
    enriched = deepcopy(result)
    payload = _structured_content(enriched) or {}
    payload["foreground_outcome"] = outcome
    payload["restoration"] = deepcopy(dict(restoration))
    if effect is not None:
        payload["effect"] = effect
    content = enriched.get("content")
    if not isinstance(content, list):
        content = []
    content.append(
        {
            "type": "text",
            "text": "Structured content:\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }
    )
    enriched["content"] = content
    enriched["structuredContent"] = payload
    return enriched


async def _settle_without_cancelling(
    operation: Awaitable[Any],
) -> tuple[Any | None, BaseException | None, asyncio.CancelledError | None]:
    """Finish one terminal operation while deferring caller cancellation."""

    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    try:
        return task.result(), None, cancellation
    except BaseException as exc:
        return None, exc, cancellation


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
                dispatched=(
                    False if code in _UPSTREAM_NOT_DISPATCHED_CODES else None
                ),
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
    delivery_mode = arguments.get("delivery_mode", "background")
    if not isinstance(delivery_mode, str) or delivery_mode not in {
        "background",
        "foreground",
    }:
        raise ComputerServerError(
            "invalid_arguments",
            'delivery_mode must be "background" or "foreground".',
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


def _validate_foreground_input(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    platform: str,
) -> None:
    if arguments.get("delivery_mode") != "foreground":
        return
    if platform != "darwin":
        raise ComputerServerError(
            "platform_unsupported",
            "Foreground Computer Use is available only on macOS.",
        )
    if tool_name not in {"get_window_state", "click"}:
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground delivery is limited to window observation and one click.",
        )
    if _window_key(arguments) is None or _window_key(arguments)[0] is None:
        raise ComputerServerError(
            "window_target_required",
            "Foreground delivery requires an exact pid and window_id.",
        )
    if tool_name != "click":
        return
    if arguments.get("button", "left") != "left":
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground delivery supports left clicks only.",
        )
    count = arguments.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count != 1:
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground delivery supports one click only.",
        )
    modifier = arguments.get("modifier", [])
    if not isinstance(modifier, list) or modifier:
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground delivery does not support modifiers.",
        )
    if arguments.get("click_mode") == "raw":
        raise ComputerServerError(
            "foreground_forbidden",
            "Raw click delivery is background-only.",
        )
    if arguments.get("action", "press") not in {"press", "click"}:
        raise ComputerServerError(
            "foreground_forbidden",
            "Foreground delivery supports a single press action only.",
        )
    element_token = arguments.get("element_token")
    has_element_token = isinstance(element_token, str) and bool(element_token.strip())
    x = arguments.get("x")
    y = arguments.get("y")
    has_coordinates = (
        not isinstance(x, bool)
        and isinstance(x, (int, float))
        and not isinstance(y, bool)
        and isinstance(y, (int, float))
    )
    capture_id = arguments.get("capture_id")
    has_capture_id = isinstance(capture_id, str) and bool(capture_id.strip())
    if has_element_token == has_coordinates:
        raise ComputerServerError(
            "target_not_admissible",
            "Foreground click requires exactly one current element or captured coordinate target.",
        )
    if has_element_token:
        if has_capture_id or any(
            arguments.get(key) is not None
            for key in ("from_zoom", "debug_image_out")
        ):
            raise ComputerServerError(
                "target_not_admissible",
                "Foreground element clicks cannot include coordinate-only arguments.",
            )
    elif not has_capture_id or arguments.get("from_zoom") is True or arguments.get(
        "debug_image_out"
    ) is not None:
        raise ComputerServerError(
            "target_not_admissible",
            "Foreground coordinate clicks require the exact current capture_id.",
        )


def _foreground_control_socket_path(state_path: Path) -> Path:
    state_directory = state_path.parent
    owner_uid = state_directory.stat().st_uid
    flavor = hashlib.sha256(os.fsencode(state_directory)).hexdigest()[
        :_SOCKET_FLAVOR_HEX_BYTES
    ]
    return (
        Path("/tmp")
        / f"avibe-cua-{owner_uid}-{flavor}"
        / _FOREGROUND_CONTROL_SOCKET
    )


class ComputerUseServer:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
        status_reader: Callable[[], ComputerUseStatus] | None = None,
        upstream_factory: Callable[[ComputerUseState], Awaitable[Upstream]] = JsonRpcUpstream.start,
        lease_manager: DesktopLeaseManager | None = None,
        foreground_control: ForegroundControl | None = None,
        platform: str | None = None,
        process_start_stamp_reader: Callable[[int], str | None] = (
            _process_start_stamp
        ),
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
        self._foreground_control = foreground_control or UnixForegroundControl(
            _foreground_control_socket_path(self._state_path)
        )
        self._platform = platform or sys.platform
        self._process_start_stamp_reader = process_start_stamp_reader

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
                session_state.window_observations.clear()
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
                    session_state.window_observations.clear()
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
        session_state.window_observations.clear()

    @staticmethod
    def _commit_session_started(
        session_state: _SessionState,
        upstream: Upstream,
        state: ComputerUseState,
        lease: Lease,
    ) -> None:
        if session_state.last_epoch is None or session_state.last_epoch != lease.epoch:
            session_state.observed_windows.clear()
            session_state.window_observations.clear()
        session_state.active_proxy_serial = upstream.serial
        session_state.active_daemon_key = state.daemon_key
        session_state.last_epoch = lease.epoch

    async def _prepare_session(
        self,
        upstream: Upstream,
        session: str,
        state: ComputerUseState,
        lease: Lease,
        session_state: _SessionState,
        *,
        explicit_start: bool,
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
            result = await self._upstream_call(
                upstream,
                "end_session",
                {"session": session},
            )
            if result.get("isError"):
                raise ComputerServerError(
                    "session_unavailable",
                    "The driver could not retire the previous computer-use session.",
                )
            self._commit_session_ended(session_state)
            active = False
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
                lease,
            )

    async def _heartbeat(self, lease: Lease) -> None:
        try:
            while True:
                await asyncio.sleep(_LEASE_HEARTBEAT_SECONDS)
                if not await asyncio.to_thread(self._lease_manager.refresh, lease):
                    raise _LeaseLost("the desktop lease changed holders")
        except asyncio.CancelledError:
            raise
        except _LeaseLost:
            raise
        except Exception as exc:
            raise _LeaseLost("the desktop lease could not be refreshed") from exc

    async def _run_while_lease_owned(
        self,
        heartbeat: asyncio.Task[None],
        operation: Awaitable[Any],
    ) -> Any:
        """Run one setup or forwarding step while observing lease loss."""

        operation_task = asyncio.ensure_future(operation)
        try:
            done, _pending = await asyncio.wait(
                (operation_task, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            raise
        if heartbeat in done:
            operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            await heartbeat
            raise _LeaseLost("the desktop lease heartbeat stopped")
        return await operation_task

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

    def _record_observation(
        self,
        result: Mapping[str, Any],
        arguments: Mapping[str, Any],
        session_state: _SessionState,
        identity_before: _WindowIdentity | None,
    ) -> None:
        key = _window_key(arguments)
        if key is None:
            return
        session_state.observed_windows.add(key)
        if key[0] is None:
            return
        payload = _structured_content(result)
        if (
            payload is None
            or identity_before is None
            or not _verify_window_identity(
                identity_before,
                payload,
                self._process_start_stamp_reader,
            )
        ):
            session_state.window_observations.pop((key[0], key[1]), None)
            return
        snapshot_id = payload.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            snapshot_id = None
        capture_id = payload.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id.strip():
            capture_id = None
        elements = payload.get("elements")
        element_tokens = (
            frozenset(
                token
                for element in elements
                if isinstance(element, Mapping)
                and isinstance((token := element.get("element_token")), str)
                and token.strip()
            )
            if isinstance(elements, list)
            else frozenset()
        )
        session_state.window_observations[(key[0], key[1])] = _WindowObservation(
            identity=identity_before,
            snapshot_id=snapshot_id,
            capture_id=capture_id,
            element_tokens=element_tokens,
        )

    @staticmethod
    def _require_foreground_observation(
        arguments: Mapping[str, Any],
        session_state: _SessionState,
    ) -> _WindowIdentity | None:
        key = _window_key(arguments)
        if key is None or key[0] is None:
            return None
        observation = session_state.window_observations.get((key[0], key[1]))
        if observation is None:
            raise ComputerServerError(
                "target_not_admissible",
                "Foreground input requires a current exact-window observation.",
            )
        element_token = arguments.get("element_token")
        if isinstance(element_token, str) and element_token.strip():
            if element_token not in observation.element_tokens:
                raise ComputerServerError(
                    "target_not_admissible",
                    "The foreground element does not belong to the current target observation.",
                )
            return observation.identity
        capture_id = arguments.get("capture_id")
        if (
            not isinstance(capture_id, str)
            or not capture_id.strip()
            or capture_id != observation.capture_id
        ):
            raise ComputerServerError(
                "target_not_admissible",
                "The foreground coordinates are not bound to the current target capture.",
            )
        return observation.identity

    async def _foreground_contract(
        self,
        upstream: Upstream,
        arguments: Mapping[str, Any],
        *,
        session: str,
        title: str,
        heartbeat: asyncio.Task[None],
        expected_target_identity: _WindowIdentity | None,
    ) -> _ForegroundContract:
        key = _window_key(arguments)
        if key is None or key[0] is None:
            raise ComputerServerError(
                "window_target_required",
                "Foreground delivery requires an exact pid and window_id.",
            )
        windows_result = await self._run_while_lease_owned(
            heartbeat,
            self._upstream_call(upstream, "list_windows", {"session": session}),
        )
        if windows_result.get("isError"):
            raise ComputerServerError(
                "target_not_admissible",
                "The target window could not be admitted from a fresh window observation.",
            )
        windows_payload = _structured_content(windows_result)
        if windows_payload is None:
            raise ComputerServerError(
                "target_not_admissible",
                "The fresh window observation was not verifiable.",
            )
        target = _window_by_key(windows_payload, key)
        if target is None:
            raise ComputerServerError(
                "target_missing",
                "The exact target window is no longer present.",
            )
        if expected_target_identity is None:
            target_identity = _record_window_identity(
                target,
                self._process_start_stamp_reader,
            )
        elif _verify_window_identity(
            expected_target_identity,
            target,
            self._process_start_stamp_reader,
        ):
            target_identity = expected_target_identity
        else:
            target_identity = None
        if target_identity is None:
            raise ComputerServerError(
                "target_not_admissible",
                "The exact target window identity changed or could not be verified.",
            )
        if target.get("on_current_space") is not True:
            raise ComputerServerError(
                "target_off_active_space",
                "The target window is not on the active Space.",
            )
        if not isinstance(target.get("is_on_screen"), bool):
            raise ComputerServerError(
                "target_not_admissible",
                "The target visibility state could not be verified.",
            )
        minimized = target.get("minimized")
        if target["is_on_screen"] is False or minimized is True:
            raise ComputerServerError(
                "target_not_admissible",
                "Hidden and minimized targets require the Route B reveal follow-up.",
            )
        active_space_id = target.get("current_space_id")
        if isinstance(active_space_id, bool) or not isinstance(active_space_id, int):
            raise ComputerServerError(
                "target_not_admissible",
                "The target active Space could not be verified.",
            )
        prior_frontmost = _frontmost_window(windows_payload)
        if prior_frontmost is None:
            raise ComputerServerError(
                "target_not_admissible",
                "The prior frontmost window could not be verified.",
            )
        prior_identity = _record_window_identity(
            prior_frontmost,
            self._process_start_stamp_reader,
        )
        if prior_identity is None:
            raise ComputerServerError(
                "target_not_admissible",
                "The prior frontmost window identity could not be verified.",
            )
        prior_window = _window_by_identity(
            windows_payload,
            prior_identity,
            self._process_start_stamp_reader,
        )
        if (
            prior_window is None
            or prior_window.get("current_space_id") != active_space_id
            or prior_window.get("on_current_space") is not True
        ):
            raise ComputerServerError(
                "target_not_admissible",
                "The prior frontmost window is not on the target's active Space.",
            )
        pointer_result = await self._run_while_lease_owned(
            heartbeat,
            self._upstream_call(upstream, "get_cursor_position", {"session": session}),
        )
        if pointer_result.get("isError"):
            raise ComputerServerError(
                "target_not_admissible",
                "The hardware pointer position could not be observed.",
            )
        pointer_payload = _structured_content(pointer_result)
        if pointer_payload is None or pointer_payload.get("available") is not True:
            raise ComputerServerError(
                "target_not_admissible",
                "The hardware pointer position was unavailable.",
            )
        pointer_x = _float_value(pointer_payload.get("x"))
        pointer_y = _float_value(pointer_payload.get("y"))
        if pointer_x is None or pointer_y is None:
            raise ComputerServerError(
                "target_not_admissible",
                "The hardware pointer position was not verifiable.",
            )
        return _ForegroundContract(
            target_identity=target_identity,
            target_label={
                "app_name": str(target.get("app_name") or ""),
                "title": str(target.get("title") or ""),
            },
            prior_frontmost=prior_identity,
            pointer={"x": pointer_x, "y": pointer_y},
            active_space_id=int(active_space_id),
        )

    async def _restore_and_verify_foreground(
        self,
        upstream: Upstream,
        contract: _ForegroundContract,
        *,
        session: str,
        title: str,
        heartbeat: asyncio.Task[None],
    ) -> _ForegroundRestoration:
        restoration = _ForegroundRestoration.from_contract(contract)
        try:
            native_result = await self._foreground_control.request(
                "restore",
                session=session,
                title=title,
                target=contract.target,
                restoration=restoration.as_dict(),
            )
        except Exception as exc:
            restoration.fail_all(
                _bounded_diagnostic(str(exc)) or "native restoration failed"
            )
        else:
            restoration.apply_native_result(native_result)

        windows_payload: dict[str, Any] | None = None
        windows_failure: str | None = None
        try:
            windows_result = await self._run_while_lease_owned(
                heartbeat,
                self._upstream_call(
                    upstream,
                    "list_windows",
                    {"session": session},
                ),
            )
            if windows_result.get("isError"):
                windows_failure = "fresh window observation failed"
            else:
                windows_payload = _structured_content(windows_result)
                if windows_payload is None:
                    windows_failure = (
                        "fresh window observation was not structured"
                    )
        except Exception as exc:
            windows_failure = (
                _bounded_diagnostic(str(exc))
                or "fresh window observation failed"
            )

        pointer_payload: dict[str, Any] | None = None
        pointer_failure: str | None = None
        try:
            pointer_result = await self._run_while_lease_owned(
                heartbeat,
                self._upstream_call(
                    upstream,
                    "get_cursor_position",
                    {"session": session},
                ),
            )
            if pointer_result.get("isError"):
                pointer_failure = "fresh pointer observation failed"
            else:
                pointer_payload = _structured_content(pointer_result)
                if (
                    pointer_payload is None
                    or pointer_payload.get("available") is not True
                ):
                    pointer_failure = "fresh pointer observation was unavailable"
        except Exception as exc:
            pointer_failure = (
                _bounded_diagnostic(str(exc))
                or "fresh pointer observation failed"
            )

        if windows_failure is not None:
            restoration.prior_frontmost.fail(windows_failure)
            restoration.active_space.fail(windows_failure)
        else:
            assert windows_payload is not None
            prior_frontmost = _frontmost_window(windows_payload)
            if (
                prior_frontmost is not None
                and _verify_window_identity(
                    contract.prior_frontmost,
                    prior_frontmost,
                    self._process_start_stamp_reader,
                )
            ):
                restoration.prior_frontmost.verified = True
            else:
                if prior_frontmost is None:
                    detail = "prior frontmost application or exact window was not restored"
                elif _window_key_from_record(prior_frontmost) != (
                    contract.prior_frontmost.pid,
                    contract.prior_frontmost.window_id,
                ):
                    detail = "prior frontmost exact window identity was not restored"
                else:
                    detail = "prior frontmost process identity changed before verification"
                restoration.prior_frontmost.fail(detail)

            prior_window = _window_by_identity(
                windows_payload,
                contract.prior_frontmost,
                self._process_start_stamp_reader,
            )
            if (
                prior_window is not None
                and prior_window.get("current_space_id")
                == contract.active_space_id
                and prior_window.get("on_current_space") is True
            ):
                restoration.active_space.verified = True
            else:
                restoration.active_space.fail(
                    "affected display active Space was not restored"
                )

        if pointer_failure is not None:
            restoration.pointer.fail(pointer_failure)
        else:
            assert pointer_payload is not None
            pointer_x = _float_value(pointer_payload.get("x"))
            pointer_y = _float_value(pointer_payload.get("y"))
            if pointer_x is None or pointer_y is None:
                restoration.pointer.fail(
                    "fresh pointer observation was not verifiable"
                )
            elif (
                abs(pointer_x - contract.pointer["x"])
                <= _FOREGROUND_POINTER_TOLERANCE
                and abs(pointer_y - contract.pointer["y"])
                <= _FOREGROUND_POINTER_TOLERANCE
            ):
                restoration.pointer.verified = True
            else:
                restoration.pointer.fail(
                    "hardware pointer position was not restored"
                )
        return restoration

    async def _finish_foreground_transaction(
        self,
        upstream: Upstream,
        contract: _ForegroundContract,
        *,
        session: str,
        title: str,
        heartbeat: asyncio.Task[None],
        reason: str,
    ) -> tuple[_ForegroundRestoration, str | None]:
        async def attention(restoration: _ForegroundRestoration) -> None:
            with suppress(Exception):
                await self._foreground_control.request(
                    "attention",
                    session=session,
                    title=title,
                    target=contract.target,
                    restoration=restoration.as_dict(),
                    reason=reason,
                )

        restoration = await self._restore_and_verify_foreground(
            upstream,
            contract,
            session=session,
            title=title,
            heartbeat=heartbeat,
        )
        if not restoration.restoration_ok:
            await attention(restoration)
            return restoration, None

        try:
            cleared = await self._foreground_control.request(
                "clear",
                session=session,
                title=title,
                target=contract.target,
                restoration=restoration.as_dict(),
                reason=reason,
            )
        except Exception as exc:
            clear_error = (
                _bounded_diagnostic(str(exc))
                or "the active foreground indicator could not be cleared"
            )
            return restoration, clear_error
        if cleared.get("ok") is not True:
            clear_error = str(
                cleared.get("error")
                or "the active foreground indicator could not be cleared"
            )
            return restoration, clear_error
        return restoration, None

    async def _call_foreground(
        self,
        upstream: Upstream,
        name: str,
        arguments: Mapping[str, Any],
        *,
        session: str,
        title: str,
        heartbeat: asyncio.Task[None],
        expected_target_identity: _WindowIdentity | None,
    ) -> dict[str, Any]:
        try:
            contract = await self._foreground_contract(
                upstream,
                arguments,
                session=session,
                title=title,
                heartbeat=heartbeat,
                expected_target_identity=expected_target_identity,
            )
        except ComputerServerError as exc:
            return _foreground_error(exc.code, str(exc))

        try:
            consent = await self._foreground_control.request(
                "request_consent",
                session=session,
                title=title,
                target=contract.target,
            )
        except (ConnectionError, OSError, asyncio.IncompleteReadError, ValueError, json.JSONDecodeError) as exc:
            return _foreground_error(
                "foreground_shell_unavailable",
                "The native shell could not receive the foreground consent request.",
                detail=_bounded_diagnostic(str(exc)),
            )
        if consent.get("ok") is not True:
            return _foreground_error(
                "foreground_shell_unavailable",
                "The native shell did not accept the foreground consent request.",
                shell_error=str(consent.get("error") or "unknown"),
            )
        if consent.get("allowed") is not True:
            return _foreground_error(
                "foreground_consent_required",
                "Foreground control requires consent from the native desktop shell.",
                session_title=title,
            )

        active, active_error, cancellation = await _settle_without_cancelling(
            self._foreground_control.request(
                "set_active",
                session=session,
                title=title,
                target=contract.target,
                restoration=_ForegroundRestoration.from_contract(
                    contract
                ).as_dict(),
            )
        )
        if active_error is not None:
            if cancellation is not None:
                raise cancellation
            exc = active_error
            return _foreground_error(
                "foreground_shell_unavailable",
                "The native shell could not prepare foreground control.",
                detail=_bounded_diagnostic(str(exc)),
            )
        assert isinstance(active, dict)
        if active.get("ok") is not True:
            if cancellation is not None:
                raise cancellation
            return _foreground_error(
                "foreground_shell_unavailable",
                "The native shell could not mark foreground control active.",
                shell_error=str(active.get("error") or "unknown"),
            )
        if cancellation is not None:
            _result, finish_error, finish_cancellation = (
                await _settle_without_cancelling(
                    self._finish_foreground_transaction(
                        upstream,
                        contract,
                        session=session,
                        title=title,
                        heartbeat=heartbeat,
                        reason="cancelled",
                    )
                )
            )
            if finish_error is not None:
                logger.warning(
                    "foreground cancellation cleanup failed: %s",
                    _bounded_diagnostic(str(finish_error))
                    or type(finish_error).__name__,
                )
            raise finish_cancellation or cancellation

        forwarded = dict(arguments)
        if name == "click":
            forwarded["delivery_mode"] = "foreground"
        else:
            forwarded.pop("delivery_mode", None)
        forwarded["session"] = session
        if name not in self._upstream_accepts_session:
            forwarded.pop("session", None)
        effect = None
        operation_cancellation: asyncio.CancelledError | None = None
        try:
            result = await self._run_while_lease_owned(
                heartbeat,
                self._upstream_call(upstream, name, forwarded),
            )
            payload = _structured_content(result)
            if isinstance(payload, dict):
                effect = payload.get("effect")
        except asyncio.CancelledError as exc:
            operation_cancellation = exc
            result = _error_result(
                "upstream_unavailable",
                "The foreground Computer Use call was cancelled after activation.",
            )
        except Exception:
            result = _error_result(
                "upstream_unavailable",
                "The foreground Computer Use call could not be completed.",
            )

        finish_result, finish_error, finish_cancellation = (
            await _settle_without_cancelling(
                self._finish_foreground_transaction(
                    upstream,
                    contract,
                    session=session,
                    title=title,
                    heartbeat=heartbeat,
                    reason=(
                        "cancelled"
                        if operation_cancellation is not None
                        else "completed"
                    ),
                )
            )
        )
        if finish_error is not None:
            restoration = _ForegroundRestoration.from_contract(contract)
            restoration.fail_all(
                _bounded_diagnostic(str(finish_error))
                or "restoration could not be completed"
            )
            indicator_error = None
        else:
            assert isinstance(finish_result, tuple)
            restoration, indicator_error = finish_result
        if operation_cancellation is not None or finish_cancellation is not None:
            raise finish_cancellation or operation_cancellation

        if not restoration.restoration_ok:
            if effect == "confirmed":
                outcome = "effect_occurred_restore_failed"
            else:
                outcome = "attempted_effect_unverifiable"
            return _error_result(
                "foreground_restore_failed",
                "Foreground control could not restore and verify the prior desktop state.",
                result_params={
                    "foreground_outcome": outcome,
                    "restoration": restoration.as_dict(),
                    "effect": effect or "unverifiable",
                },
            )

        outcome = (
            "attempted_effect_unverifiable"
            if effect != "confirmed"
            else "effect_occurred_restore_verified"
        )
        if indicator_error is not None:
            return _error_result(
                "foreground_shell_unavailable",
                "Foreground control restored the desktop, but the native indicator could not be cleared.",
                result_params={
                    "foreground_outcome": outcome,
                    "restoration": restoration.as_dict(),
                    "effect": effect or "unverifiable",
                    "shell_error": indicator_error,
                },
            )

        if result.get("isError"):
            return _with_foreground_outcome(
                result,
                outcome="attempted_effect_unverifiable",
                restoration=restoration.as_dict(),
                effect=effect or "unverifiable",
            )
        return _with_foreground_outcome(
            result,
            outcome=outcome,
            restoration=restoration.as_dict(),
            effect=effect or "unverifiable",
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        handle_descriptor: Any = _NO_SCRIPT_HANDLE,
        session_title: str | None = None,
    ) -> dict[str, Any]:
        session = arguments.get("session")
        if not isinstance(session, str) or not session.strip():
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
                dispatched=False,
            )
        session = session.strip()
        if name not in COMPUTER_USE_TOOL_NAMES:
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
                _validate_foreground_input(
                    name,
                    arguments,
                    platform=self._platform,
                )
            except ComputerServerError as exc:
                if arguments.get("delivery_mode") == "foreground":
                    return _foreground_error(
                        exc.code,
                        str(exc),
                        dispatched=False,
                    )
                return _error_result(exc.code, str(exc), dispatched=False)

            for admission_attempt in range(_ADMISSION_RETRY_LIMIT + 1):
                acquisition: LeaseAcquisition | None = None
                upstream: Upstream | None = None
                heartbeat: asyncio.Task[None] | None = None
                pre_forward = True
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
                    acquisition = await asyncio.to_thread(
                        self._lease_manager.acquire,
                        session,
                        state.daemon_key,
                        self._current_ready_daemon_key,
                    )
                    lease = acquisition.lease
                    heartbeat = asyncio.create_task(
                        self._heartbeat(lease),
                        name=f"computer-use-lease-heartbeat-{session}",
                    )
                    upstream = await self._run_while_lease_owned(
                        heartbeat,
                        self._ensure_upstream(state),
                    )
                    await self._run_while_lease_owned(
                        heartbeat,
                        asyncio.to_thread(
                            self._revalidate_daemon_key,
                            state.daemon_key,
                        ),
                    )
                    session_state = self._sessions.setdefault(
                        session,
                        _SessionState(),
                    )
                    await self._run_while_lease_owned(
                        heartbeat,
                        self._prepare_session(
                            upstream,
                            session,
                            state,
                            lease,
                            session_state,
                            explicit_start=name == "start_session",
                        ),
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
                            return await self._upstream_call(
                                upstream,
                                discovery_tool,
                                forwarded_discovery,
                            )

                        try:
                            validation_error = await self._run_while_lease_owned(
                                heartbeat,
                                validate_handle_is_current(
                                    parsed_handle,
                                    name,
                                    discover,
                                ),
                            )
                        except ComputerScriptHandleError as exc:
                            return _error_result(
                                exc.code,
                                dispatched=False,
                                result_params=exc.params,
                            )
                        if validation_error is not None:
                            return _not_dispatched_error_result(validation_error)
                    self._require_observation(name, arguments, session_state)
                    foreground_target_identity: _WindowIdentity | None = None
                    if (
                        arguments.get("delivery_mode") == "foreground"
                        and name == "click"
                    ):
                        foreground_target_identity = (
                            self._require_foreground_observation(
                                arguments,
                                session_state,
                            )
                        )
                    observation_identity_before: _WindowIdentity | None = None
                    if name in _OBSERVE_TOOLS:
                        observation_key = _window_key(arguments)
                        if (
                            observation_key is not None
                            and observation_key[0] is not None
                        ):
                            observation_identity_before = (
                                _record_window_identity(
                                    {
                                        "pid": observation_key[0],
                                        "window_id": observation_key[1],
                                    },
                                    self._process_start_stamp_reader,
                                )
                            )
                    await self._run_while_lease_owned(
                        heartbeat,
                        asyncio.sleep(0),
                    )
                    pre_forward = False

                    if arguments.get("delivery_mode") == "foreground":
                        result = await self._call_foreground(
                            upstream,
                            name,
                            arguments,
                            session=session,
                            title=session_title or session,
                            heartbeat=heartbeat,
                            expected_target_identity=foreground_target_identity,
                        )
                    else:
                        forwarded = dict(arguments)
                        forwarded["session"] = session
                        if name not in self._upstream_accepts_session:
                            forwarded.pop("session", None)
                        result = await self._run_while_lease_owned(
                            heartbeat,
                            self._upstream_call(
                                upstream,
                                name,
                                forwarded,
                            ),
                        )

                    if name in _OBSERVE_TOOLS and not result.get("isError"):
                        self._record_observation(
                            result,
                            arguments,
                            session_state,
                            observation_identity_before,
                        )
                    if name == "start_session":
                        if result.get("isError"):
                            if acquisition.newly_claimed:
                                await asyncio.to_thread(
                                    self._lease_manager.release,
                                    lease,
                                )
                        else:
                            self._commit_session_started(
                                session_state,
                                upstream,
                                state,
                                lease,
                            )
                    elif name == "end_session" and not result.get("isError"):
                        self._commit_session_ended(session_state)
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            lease,
                        )
                        await self.revoke_foreground_consent(session)
                        self._sessions.pop(session, None)
                        self._session_locks.pop(session, None)
                    return result
                except _LeaseLost:
                    if upstream is not None:
                        await self._discard_upstream(upstream)
                    if (
                        acquisition is not None
                        and pre_forward
                        and acquisition.newly_claimed
                    ):
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
                    return _error_result(
                        "desktop_lease_lost",
                        "The desktop lease changed before computer use completed.",
                        dispatched=False if pre_forward else None,
                    )
                except _DaemonGenerationChanged:
                    if upstream is not None:
                        await self._discard_upstream(upstream)
                    if acquisition is not None and acquisition.newly_claimed:
                        # A transient not-ready status can use this same path
                        # without changing the daemon key. Keep a lease this
                        # call merely renewed; the next admission invalidates
                        # it under the lock if the generation really changed.
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
                    if admission_attempt < _ADMISSION_RETRY_LIMIT:
                        continue
                    return _error_result(
                        "computer_use_unavailable",
                        "Computer use changed while the request was being admitted.",
                        dispatched=False,
                        status="unavailable",
                        reason="daemon_generation_changed",
                    )
                except ComputerServerError as exc:
                    if (
                        acquisition is not None
                        and pre_forward
                        and acquisition.newly_claimed
                    ):
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
                    if arguments.get("delivery_mode") == "foreground":
                        return _foreground_error(
                            exc.code,
                            str(exc),
                            dispatched=False if pre_forward else None,
                        )
                    return _error_result(
                        exc.code,
                        str(exc),
                        dispatched=False if pre_forward else None,
                    )
                except asyncio.CancelledError:
                    if (
                        acquisition is not None
                        and pre_forward
                        and acquisition.newly_claimed
                    ):
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
                    raise
                except UpstreamUnavailable as exc:
                    if upstream is not None:
                        await self._discard_upstream(upstream)
                    if (
                        acquisition is not None
                        and pre_forward
                        and acquisition.newly_claimed
                    ):
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
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
                    if (
                        acquisition is not None
                        and pre_forward
                        and acquisition.newly_claimed
                    ):
                        await asyncio.to_thread(
                            self._lease_manager.release,
                            acquisition.lease,
                        )
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
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        with suppress(BaseException):
                            await heartbeat

            raise AssertionError("computer-use admission loop exhausted")

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
            await self.revoke_foreground_consent(normalized)
            return {"isError": False, "content": []}
        result = await self.call_tool("end_session", {"session": normalized})
        await self.revoke_foreground_consent(normalized)
        return result

    async def revoke_foreground_consent(self, session: str) -> None:
        normalized = str(session or "").strip()
        if not normalized:
            return
        with suppress(
            ConnectionError,
            OSError,
            asyncio.IncompleteReadError,
            ValueError,
            json.JSONDecodeError,
        ):
            await self._foreground_control.request(
                "revoke_session",
                session=normalized,
                title=normalized,
            )

    async def retire_session(self, session: str) -> dict[str, Any]:
        """Release all Controller-owned Computer Use state for an archived Session."""

        normalized = str(session or "").strip()
        if not normalized:
            return _error_result(
                "session_required",
                "Every computer-use call requires the current Avibe session id.",
            )

        result = await self.end_session_if_active(normalized)
        lock = self._session_locks.get(normalized)
        if lock is not None:
            async with lock:
                self._sessions.pop(normalized, None)
                if self._session_locks.get(normalized) is lock:
                    self._session_locks.pop(normalized, None)
        await asyncio.to_thread(self._lease_manager.release_holder, normalized)
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
