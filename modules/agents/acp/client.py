"""Controller-independent stdio NDJSON JSON-RPC 2.0 client for ACP servers."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from asyncio.subprocess import Process
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.process_isolation import (
    KILL_SIGNAL,
    PROCESS_IDENTITY_ENV,
    PersistedProcessIdentity,
    capture_spawned_process_identity,
    fingerprint_process_marker,
    isolated_subprocess_kwargs,
    new_process_identity_marker,
    process_identity_subprocess_env,
    reap_marked_processes,
    reap_orphaned_process_tree,
    signal_process_tree,
)

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = 1
STREAM_BUFFER_LIMIT = 64 * 1024 * 1024  # 64 MiB per NDJSON frame
_STDERR_TAIL_LINES = 64
_STDERR_LINE_MAX_CHARS = 2000
_EOF_REAP_GRACE_SECONDS = 0.5

JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603
ACP_AUTH_REQUIRED = -32000

NotificationCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class AcpTransportError(RuntimeError):
    """Transport, subprocess lifecycle, framing, or timeout failure."""


class AcpRpcError(RuntimeError):
    """Structured JSON-RPC 2.0 error returned by an ACP peer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"ACP RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def _capability_enabled(value: Any) -> bool:
    """Interpret an ACP capability value (bool or presence object such as ``{}``)."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    return isinstance(value, Mapping)


def _reap_process_tree(
    identity: PersistedProcessIdentity | None,
    fingerprint: str | None,
    terminate_timeout: float,
) -> None:
    """Reap any surviving descendants in the ACP process tree after the leader exits."""
    if identity is not None:
        reap_orphaned_process_tree(logger, "ACP server", expected_identity=identity)
    if fingerprint is not None:
        reap_marked_processes(
            logger,
            "ACP server",
            worker_fingerprint=fingerprint,
            terminate_timeout=terminate_timeout,
        )


class AcpClient:
    """Async NDJSON JSON-RPC 2.0 client over a single stdio ACP subprocess.

    One background reader task reads stdout and dispatches responses, ordered
    notifications, and concurrent server-initiated requests.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        request_timeout: float = 120.0,
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self._command = tuple(command)
        self._cwd = str(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self._request_timeout = request_timeout

        self._process: Process | None = None
        self._process_identity: PersistedProcessIdentity | None = None
        self._process_fingerprint: str | None = None
        self._request_id = 0
        # Maps JSON-RPC request id -> Future resolving to (result, rpc_error, notify_barrier)
        self._pending: dict[int | str, asyncio.Future[tuple[dict[str, Any], AcpRpcError | None, int]]] = {}
        self._write_lock = asyncio.Lock()

        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._notify_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

        self._notification_cb: NotificationCallback | None = None
        self._server_request_cb: ServerRequestCallback | None = None

        self._notify_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._notify_enqueued = 0
        self._notify_completed = 0
        self._notify_stopped = False
        self._notify_cond = asyncio.Condition()

        # Negotiated state populated by initialize()
        self._agent_capabilities: dict[str, Any] = {}
        self._agent_info: dict[str, Any] = {}
        self._auth_methods: tuple[dict[str, Any], ...] = ()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """Spawn the ACP subprocess and start background stdio tasks."""
        if self._process is not None and self._process.returncode is None:
            raise AcpTransportError("ACP client is already running")

        self._stderr_tail.clear()
        self._notify_enqueued = 0
        self._notify_completed = 0
        self._notify_stopped = False
        self._notify_queue = asyncio.Queue()
        self._notify_cond = asyncio.Condition()
        self._agent_capabilities = {}
        self._agent_info = {}
        self._auth_methods = ()

        self._process_identity = None
        self._process_fingerprint = None

        marker = new_process_identity_marker()
        env = dict(self._env) if self._env is not None else process_identity_subprocess_env(marker)
        env[PROCESS_IDENTITY_ENV] = marker

        subprocess_kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": STREAM_BUFFER_LIMIT,
            "env": env,
            **isolated_subprocess_kwargs(),
        }
        if self._cwd is not None:
            subprocess_kwargs["cwd"] = self._cwd

        self._process_fingerprint = fingerprint_process_marker(marker)
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(*self._command, **subprocess_kwargs))
        try:
            self._process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            self._process = await spawn
            self._process_identity = capture_spawned_process_identity(self._process.pid, marker)
            await self.close()
            raise
        except OSError as exc:
            self._process_fingerprint = None
            raise AcpTransportError(f"Failed to launch ACP server {self._command[0]!r}: {exc}") from exc

        self._process_identity = capture_spawned_process_identity(self._process.pid, marker)

        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        self._notify_task = asyncio.create_task(self._notify_worker())

    async def close(self, *, graceful_timeout: float = 5.0, terminate_timeout: float = 3.0) -> None:
        """Shut down the subprocess tree and cancel background tasks."""
        self._fail_pending(AcpTransportError("ACP client closed"))

        proc = self._process
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=graceful_timeout)
            except asyncio.TimeoutError:
                logger.warning("ACP server did not exit after stdin EOF; sending SIGTERM")
                signal_process_tree(proc, signal.SIGTERM, logger, "ACP server")
                try:
                    await asyncio.wait_for(proc.wait(), timeout=terminate_timeout)
                except asyncio.TimeoutError:
                    logger.warning("ACP server did not exit after SIGTERM; sending KILL")
                    signal_process_tree(proc, KILL_SIGNAL, logger, "ACP server")
                    await proc.wait()

        identity = self._process_identity
        fingerprint = self._process_fingerprint
        self._process_identity = None
        self._process_fingerprint = None
        if identity is not None or fingerprint is not None:
            await asyncio.to_thread(_reap_process_tree, identity, fingerprint, terminate_timeout)

        tasks: list[asyncio.Task[None]] = []
        for task in (self._reader_task, self._stderr_task, self._notify_task, *self._server_request_tasks):
            if task is not None and not task.done():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._reader_task = None
        self._stderr_task = None
        self._notify_task = None
        self._server_request_tasks.clear()
        self._fail_pending(AcpTransportError("ACP client closed"))

    @property
    def is_alive(self) -> bool:
        if self._process is None or self._process.returncode is not None:
            return False
        if self._reader_task is not None and self._reader_task.done():
            return False
        return True

    @property
    def pid(self) -> int | None:
        proc = self._process
        return proc.pid if proc is not None and proc.returncode is None else None

    # ------------------------------------------------------------------
    # Capabilities from initialize()
    # ------------------------------------------------------------------

    @property
    def agent_info(self) -> dict[str, Any]:
        return dict(self._agent_info)

    @property
    def auth_methods(self) -> tuple[dict[str, Any], ...]:
        return self._auth_methods

    def _session_capabilities(self) -> Mapping[str, Any]:
        caps = self._agent_capabilities.get("sessionCapabilities")
        return caps if isinstance(caps, Mapping) else {}

    def _prompt_capabilities(self) -> Mapping[str, Any]:
        caps = self._agent_capabilities.get("promptCapabilities")
        return caps if isinstance(caps, Mapping) else {}

    def _auth_capabilities(self) -> Mapping[str, Any]:
        caps = self._agent_capabilities.get("auth")
        return caps if isinstance(caps, Mapping) else {}

    @property
    def supports_resume(self) -> bool:
        return _capability_enabled(self._session_capabilities().get("resume"))

    @property
    def supports_list(self) -> bool:
        return _capability_enabled(self._session_capabilities().get("list"))

    @property
    def supports_fork(self) -> bool:
        return _capability_enabled(self._session_capabilities().get("fork"))

    @property
    def supports_close(self) -> bool:
        return _capability_enabled(self._session_capabilities().get("close"))

    @property
    def supports_logout(self) -> bool:
        return _capability_enabled(self._auth_capabilities().get("logout"))

    @property
    def supports_images(self) -> bool:
        return _capability_enabled(self._prompt_capabilities().get("image"))

    @property
    def supports_additional_directories(self) -> bool:
        return _capability_enabled(self._session_capabilities().get("additionalDirectories"))

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def on_notification(self, callback: NotificationCallback | None) -> None:
        """Register the async callback for server notifications (e.g. ``session/update``)."""
        self._notification_cb = callback

    def on_server_request(self, callback: ServerRequestCallback | None) -> None:
        """Register the async handler for server-initiated JSON-RPC requests.

        Raise ``AcpRpcError`` from the callback to return a structured JSON-RPC
        error (such as ``-32601 Method not found``). When no callback is
        registered, all server requests are rejected with ``-32601``.
        """
        self._server_request_cb = callback

    # ------------------------------------------------------------------
    # ACP Protocol Methods
    # ------------------------------------------------------------------

    async def initialize(
        self,
        *,
        protocol_version: int = DEFAULT_PROTOCOL_VERSION,
        client_info: Mapping[str, Any] | None = None,
        client_capabilities: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "protocolVersion": protocol_version,
            "clientCapabilities": dict(client_capabilities) if client_capabilities is not None else {},
        }
        if client_info is not None:
            params["clientInfo"] = dict(client_info)

        result = await self.send_request("initialize", params, timeout=self._request_timeout)

        raw_caps = result.get("agentCapabilities")
        self._agent_capabilities = dict(raw_caps) if isinstance(raw_caps, Mapping) else {}
        raw_info = result.get("agentInfo")
        self._agent_info = dict(raw_info) if isinstance(raw_info, Mapping) else {}
        raw_auth = result.get("authMethods")
        self._auth_methods = (
            tuple(dict(item) for item in raw_auth if isinstance(item, Mapping))
            if isinstance(raw_auth, list)
            else ()
        )
        return result

    async def authenticate(self, method_id: str) -> dict[str, Any]:
        return await self.send_request("authenticate", {"methodId": method_id}, timeout=self._request_timeout)

    async def logout(self) -> dict[str, Any]:
        return await self.send_request("logout", {}, timeout=self._request_timeout)

    @staticmethod
    def _session_params(cwd, mcp_servers, additional_directories, session_id=None) -> dict[str, Any]:
        params = {"cwd": cwd, "mcpServers": [dict(server) for server in mcp_servers]}
        if session_id is not None:
            params["sessionId"] = session_id
        if additional_directories is not None:
            params["additionalDirectories"] = list(additional_directories)
        return params

    async def session_new(
        self,
        cwd: str,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        additional_directories: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        params = self._session_params(cwd, mcp_servers, additional_directories)
        return await self.send_request("session/new", params, timeout=self._request_timeout)

    async def session_resume(
        self,
        session_id: str,
        cwd: str,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        additional_directories: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        params = self._session_params(cwd, mcp_servers, additional_directories, session_id)
        return await self.send_request("session/resume", params, timeout=self._request_timeout)

    async def session_fork(
        self,
        session_id: str,
        cwd: str,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        additional_directories: Sequence[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        params = self._session_params(cwd, mcp_servers, additional_directories, session_id)
        effective_timeout = self._request_timeout if timeout is None else timeout
        return await self.send_request("session/fork", params, timeout=effective_timeout)

    async def session_list(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = cwd
        if cursor is not None:
            params["cursor"] = cursor
        return await self.send_request("session/list", params, timeout=self._request_timeout)

    async def session_close(self, session_id: str) -> dict[str, Any]:
        return await self.send_request("session/close", {"sessionId": session_id}, timeout=self._request_timeout)

    async def session_prompt(
        self,
        session_id: str,
        prompt: Sequence[Mapping[str, Any]],
        *,
        timeout: float | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sessionId": session_id,
            "prompt": [dict(block) for block in prompt],
        }
        return await self.send_request("session/prompt", params, timeout=timeout, before_write=before_write)

    async def session_cancel(self, session_id: str) -> None:
        await self.send_notification("session/cancel", {"sessionId": session_id})

    async def session_set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: Any,
    ) -> dict[str, Any]:
        return await self.send_request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=self._request_timeout,
        )


    # ------------------------------------------------------------------
    # Low-level JSON-RPC Transport
    # ------------------------------------------------------------------

    async def send_request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Send a request; ``before_write`` may reject it under the write lock before any bytes."""
        if not self.is_alive:
            raise AcpTransportError("ACP client is not running")

        self._request_id += 1
        req_id = self._request_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = dict(params)

        fut: asyncio.Future[tuple[dict[str, Any], AcpRpcError | None, int]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[req_id] = fut

        try:
            await self._write_message(msg, before_write=before_write)
        except BaseException:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise

        try:
            if timeout is None:
                result, rpc_error, barrier = await fut
            else:
                result, rpc_error, barrier = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise AcpTransportError(f"ACP RPC {method} (id={req_id}) timed out after {timeout}s") from exc

        await self._wait_for_notifications(barrier)
        if rpc_error is not None:
            raise rpc_error
        return result

    async def send_notification(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC 2.0 notification (no response expected)."""
        if not self.is_alive:
            raise AcpTransportError("ACP client is not running")

        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = dict(params)
        await self._write_message(msg)

    async def _write_message(
        self, msg: dict[str, Any], *, before_write: Callable[[], None] | None = None
    ) -> None:
        proc = self._process
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise AcpTransportError("ACP server stdin is not available")
        payload = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            async with self._write_lock:
                if before_write is not None:
                    before_write()
                proc.stdin.write(payload)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise AcpTransportError(f"Failed to write to ACP server stdin: {exc}") from exc

    async def _send_response(self, req_id: int | str, result: dict[str, Any]) -> None:
        await self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send_error(
        self,
        req_id: int | str,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write_message({"jsonrpc": "2.0", "id": req_id, "error": error})

    # ------------------------------------------------------------------
    # Background Reader & Dispatch
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure: AcpTransportError | None = None
        cancelled = False
        try:
            while True:
                try:
                    raw = await self._process.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    failure = AcpTransportError(
                        f"ACP server stdout frame exceeded {STREAM_BUFFER_LIMIT}-byte limit: {exc}"
                    )
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON line from ACP stdout: %s", line[:200])
                    continue
                if not isinstance(msg, dict):
                    logger.debug("Ignoring non-object JSON frame from ACP stdout: %r", type(msg).__name__)
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            logger.exception("ACP reader loop crashed")
            failure = AcpTransportError(f"ACP reader loop failed: {exc}")
        finally:
            if not cancelled:
                if failure is None:
                    failure = await self._build_eof_error()
                self._fail_pending(failure)

    async def _build_eof_error(self) -> AcpTransportError:
        proc = self._process
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_EOF_REAP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        if proc is not None and proc.returncode is not None and self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=_EOF_REAP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass

        if proc is not None and proc.returncode is not None:
            detail = f"ACP server exited with code {proc.returncode}"
        else:
            detail = "ACP server stdout closed unexpectedly"
        if self._stderr_tail:
            detail = f"{detail}: {self._stderr_tail[-1]}"
        return AcpTransportError(detail)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        # 1. Response to one of our outgoing requests
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            barrier = self._notify_enqueued
            if "error" in msg and msg["error"] is not None:
                raw_error = msg["error"]
                if isinstance(raw_error, Mapping):
                    code = raw_error.get("code")
                    code_int = code if isinstance(code, int) and not isinstance(code, bool) else JSONRPC_INTERNAL_ERROR
                    message = str(raw_error.get("message", "Unknown ACP RPC error"))
                    data = raw_error.get("data")
                else:
                    code_int = JSONRPC_INTERNAL_ERROR
                    message = str(raw_error)
                    data = None
                fut.set_result(({}, AcpRpcError(code_int, message, data), barrier))
            else:
                raw_result = msg.get("result")
                result = dict(raw_result) if isinstance(raw_result, Mapping) else {}
                fut.set_result((result, None, barrier))
            return

        method = msg.get("method")
        if not isinstance(method, str) or not method:
            logger.debug("Ignoring unrecognized ACP message: %s", str(msg)[:200])
            return

        raw_params = msg.get("params")
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}

        # 2. Server-initiated request (has non-null id + method) -> concurrent task
        if msg.get("id") is not None:
            req_id = msg["id"]
            task = asyncio.create_task(self._handle_server_request(req_id, method, params))
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_tasks.discard)
            return

        # 3. Notification (no id, has method) -> ordered queue
        if self._notification_cb is not None:
            self._notify_enqueued += 1
            self._notify_queue.put_nowait((method, params))

    async def _handle_server_request(self, req_id: int | str, method: str, params: dict[str, Any]) -> None:
        cb = self._server_request_cb
        if cb is None:
            await self._safe_send_error(req_id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")
            return
        try:
            result = await cb(method, params)
        except AcpRpcError as err:
            await self._safe_send_error(req_id, err.code, err.message, err.data)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Error handling ACP server request %s", method)
            await self._safe_send_error(req_id, JSONRPC_INTERNAL_ERROR, str(exc))
            return

        try:
            await self._send_response(req_id, result)
        except AcpTransportError:
            logger.debug("Could not write ACP response for server request %s (id=%s): transport closed", method, req_id)

    async def _safe_send_error(
        self,
        req_id: int | str,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        try:
            await self._send_error(req_id, code, message, data)
        except AcpTransportError:
            logger.debug("Could not write ACP error for server request id=%s: transport closed", req_id)

    async def _notify_worker(self) -> None:
        try:
            while True:
                method, params = await self._notify_queue.get()
                try:
                    cb = self._notification_cb
                    if cb is not None:
                        await cb(method, params)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Error in ACP notification callback for %s", method)
                finally:
                    async with self._notify_cond:
                        self._notify_completed += 1
                        self._notify_cond.notify_all()
        except asyncio.CancelledError:
            return
        finally:
            async with self._notify_cond:
                self._notify_stopped = True
                self._notify_cond.notify_all()

    async def _wait_for_notifications(self, barrier: int) -> None:
        if self._notify_completed >= barrier or self._notify_stopped:
            return
        if asyncio.current_task() is self._notify_task:
            return
        async with self._notify_cond:
            await self._notify_cond.wait_for(
                lambda: self._notify_completed >= barrier or self._notify_stopped
            )

    async def _stderr_reader(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self._stderr_tail.append(decoded[:_STDERR_LINE_MAX_CHARS])
        except asyncio.CancelledError:
            return

    def _fail_pending(self, error: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()
