"""JSON-RPC 2.0 transport over a persistent codex app-server subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from asyncio.subprocess import Process
from typing import Any, Awaitable, Callable, Optional

from core.process_diagnostics import log_process_snapshot, process_identity
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree

logger = logging.getLogger(__name__)

STREAM_BUFFER_LIMIT = 128 * 1024 * 1024  # 128 MB


class CodexTransport:
    """Manages a persistent ``codex app-server`` subprocess.

    Communication uses JSON-RPC 2.0 over stdin (requests/notifications from
    client) and stdout (responses, notifications, and requests from server).
    """

    def __init__(
        self,
        binary: str,
        cwd: str,
        extra_args: list[str] | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = cwd
        self._extra_args = extra_args or []
        self._process: Optional[Process] = None
        self._request_id: int = 0
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._initialized = False
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None

        # Callbacks
        self._notification_cb: Optional[Callable[[str, dict[str, Any]], Awaitable[None]]] = None
        self._server_request_cb: Optional[Callable[[int | str, str, dict[str, Any]], Awaitable[dict[str, Any]]]] = None

        # Ordered notification queue
        self._notify_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._notify_task: Optional[asyncio.Task[None]] = None
        self._notify_inflight = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the app-server and perform the ``initialize`` handshake."""
        if self._process and self._process.returncode is None:
            logger.warning("CodexTransport.start() called but process is already running")
            return

        cmd = [self._binary, "--dangerously-bypass-approvals-and-sandbox", "app-server"] + self._extra_args
        logger.info("Launching Codex app-server: %s (cwd=%s)", " ".join(cmd), self._cwd)

        if not os.path.exists(self._cwd):
            os.makedirs(self._cwd, exist_ok=True)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            limit=STREAM_BUFFER_LIMIT,
            **isolated_subprocess_kwargs(),
        )
        identity = process_identity(self._process.pid)
        logger.info(
            "Codex app-server started (pid=%s pgid=%s sid=%s service_pgid=%s)",
            self._process.pid,
            identity.get("pgid"),
            identity.get("sid"),
            os.getpgrp() if hasattr(os, "getpgrp") else None,
        )
        log_process_snapshot(logger, "codex-app-server-start", pid=self._process.pid, limit=10)

        # Start background readers
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_reader())
        self._notify_task = asyncio.create_task(self._notify_worker())

        # Perform initialize handshake
        try:
            resp = await self.send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "vibe-remote",
                        "version": "1.0.0",
                    },
                },
            )
            logger.info("Codex app-server initialized: %s", resp)
            await self.send_notification("initialized")
            self._initialized = True
        except Exception:
            logger.exception("Codex app-server handshake failed, cleaning up")
            await self.stop()
            raise

    async def stop(self) -> None:
        """Gracefully shut down the app-server process."""
        self._initialized = False
        proc = self._process
        if not proc or proc.returncode is not None:
            self._cleanup_tasks()
            return

        # Close stdin to signal EOF
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()

        # Wait briefly for graceful exit
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Codex app-server did not exit gracefully, sending SIGTERM")
            try:
                signal_process_tree(proc, signal.SIGTERM, logger, "Codex app-server")
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                logger.warning("Codex app-server did not respond to SIGTERM, sending SIGKILL")
                try:
                    signal_process_tree(proc, KILL_SIGNAL, logger, "Codex app-server")
                    await proc.wait()
                except ProcessLookupError:
                    pass

        self._cleanup_tasks()
        # Fail all pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Transport stopped"))
        self._pending.clear()
        logger.info("Codex app-server stopped")

    def _cleanup_tasks(self) -> None:
        for task in (self._reader_task, self._stderr_task, self._notify_task):
            if task and not task.done():
                task.cancel()
        self._reader_task = None
        self._stderr_task = None
        self._notify_task = None

    @property
    def is_alive(self) -> bool:
        if self._process is None or self._process.returncode is not None:
            return False
        # If the stdout reader has exited, the subprocess can still accept
        # writes but no responses will ever be dispatched to pending callers.
        if self._reader_task is not None and self._reader_task.done():
            return False
        return True

    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.is_alive

    @property
    def has_pending_notifications(self) -> bool:
        """Whether an already-read notification can still deliver a terminal."""

        return self._notify_inflight > 0 or not self._notify_queue.empty()

    @property
    def pid(self) -> Optional[int]:
        """OS pid of the codex app-server subprocess, or ``None`` if not running.

        Read-only accessor for observability (the running-agents snapshot). One
        transport (hence one pid) serves every session sharing its working dir.
        """
        proc = self._process
        if proc is None or proc.returncode is not None:
            return None
        return proc.pid

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def on_notification(
        self,
        callback: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._notification_cb = callback

    def on_server_request(
        self,
        callback: Callable[[int | str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        self._server_request_cb = callback

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the result (blocking)."""
        if not self.is_alive:
            raise ConnectionError("Codex app-server transport is not available")

        self._request_id += 1
        req_id = self._request_id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        try:
            await self._write_message(msg)
        except Exception:
            # Clean up the pending future so it doesn't leak
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.set_exception(ConnectionError(f"Failed to send {method}"))
            raise

        try:
            return await asyncio.wait_for(fut, timeout=120.0)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._initialized = False
            raise TimeoutError(f"Codex RPC {method} (id={req_id}) timed out after 120s")

    async def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (fire-and-forget)."""
        if not self.is_alive:
            raise ConnectionError("Codex app-server transport is not available")

        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write_message(msg)

    async def _write_message(self, msg: dict[str, Any]) -> None:
        proc = self._process
        if not proc or not proc.stdin or proc.stdin.is_closing():
            raise ConnectionError("Codex app-server stdin is not available")
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(line.encode())
            await proc.stdin.drain()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """Read stdout line-by-line and dispatch JSON-RPC messages."""
        assert self._process and self._process.stdout
        try:
            while True:
                try:
                    raw = await self._process.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError) as err:
                    logger.error("Codex stdout buffer error: %s", err)
                    break
                if not raw:
                    break  # EOF
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON from Codex stdout: %s", line[:200])
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Codex reader loop crashed")
        finally:
            self._initialized = False
            # Process ended — fail pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Codex app-server stdout closed"))
            self._pending.clear()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route a parsed JSON-RPC message to the right handler."""
        # Response to our request
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"Codex RPC error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result", {}))
            return

        method = msg.get("method")
        params = msg.get("params", {})

        # Server request (has id + method, expects a response)
        if "id" in msg and method:
            req_id = msg["id"]
            if self._server_request_cb:
                try:
                    result = await self._server_request_cb(req_id, method, params)
                    await self._send_response(req_id, result)
                except Exception as err:
                    logger.error("Error handling server request %s: %s", method, err)
                    await self._send_error(req_id, str(err))
            else:
                logger.warning("No handler for server request: %s", method)
                await self._send_error(req_id, "No handler registered")
            return

        # Notification (no id, has method)
        if method:
            if self._notification_cb:
                # Queue to preserve ordering instead of fire-and-forget
                self._notify_queue.put_nowait((method, params))
            return

        logger.debug("Unrecognized Codex message: %s", str(msg)[:200])

    async def _notify_worker(self) -> None:
        """Process notifications in order from the queue."""
        try:
            while True:
                method, params = await self._notify_queue.get()
                self._notify_inflight += 1
                try:
                    assert self._notification_cb is not None
                    await self._notification_cb(method, params)
                except Exception:
                    logger.exception("Error in notification handler for %s", method)
                finally:
                    self._notify_inflight -= 1
        except asyncio.CancelledError:
            return

    async def _send_response(self, req_id: int | str, result: dict[str, Any]) -> None:
        await self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send_error(self, req_id: int | str, message: str) -> None:
        await self._write_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": message}})

    async def _stderr_reader(self) -> None:
        """Drain stderr and log it."""
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                decoded = line.decode(errors="ignore").rstrip()
                logger.info("Codex stderr: %s", decoded[:2000])
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Codex stderr reader crashed")
