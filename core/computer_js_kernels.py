"""Controller-owned persistent JavaScript kernels for ``vibe computer js``.

One Node process per Avibe Session keeps top-level bindings between calls.
The kernel holds no desktop capability of its own: every call opens an
ordinary exact-Turn script connection (``ComputerScriptSessionManager``) and
the kernel uses that token only while the call executes. The Controller's
existing ``/internal/computer-script`` routes therefore keep every safety rule
(target locks, observe-first, handle descriptors, confirm).

Lifecycle, all owned here:

- started lazily by the first call; a call that started a kernel reports
  ``kernel_fresh: true`` so the agent knows earlier bindings are gone;
- calls in one Session are serialized;
- a call is cancelled at its timeout or when its Turn ends; a kernel that does
  not settle the cancel is killed and the call reports ``js_kernel_reset``;
- killed on ``js-reset``, idle, Session archive, Computer Use off, and service
  shutdown. The kernel also exits when the Controller's socket closes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import itertools
import json
import logging
import os
from pathlib import Path
import signal
import socket
import tempfile
from typing import TYPE_CHECKING, Any, Callable

from vibe.computer_script_runner import (
    _failure_envelope,
    _prune_retained_artifact_runs,
    _sdk_directory,
)

if TYPE_CHECKING:
    from core.computer_script_sessions import ComputerScriptSessionManager


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 300_000
MAX_CODE_BYTES = 1024 * 1024
IDLE_SECONDS = 15 * 60
KILL_GRACE_SECONDS = 2.0
SPAWN_TIMEOUT_SECONDS = 10.0
STATUS_POLL_SECONDS = 15.0
_BINDING_GRACE_SECONDS = 5.0
_MESSAGE_LIMIT_BYTES = 16 * 1024 * 1024
_ARTIFACT_PREFIX = "avibe-computer-js-"
_KERNEL_FD_ENV = "AVIBE_COMPUTER_KERNEL_FD"
_KERNEL_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ")
_RESET_MESSAGE = "The JavaScript kernel was reset; all bindings were discarded."


class _KernelGone(RuntimeError):
    """The kernel process ended before the call settled."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _envelope(
    code: str,
    message: str,
    *,
    name: str = "ControllerError",
    params: dict[str, Any] | None = None,
    ambiguous: bool = False,
) -> dict[str, Any]:
    return _failure_envelope(
        name=name,
        code=code,
        message=message,
        params=params or {},
        step="script",
        last_completed_step=None,
        screenshot_path=None,
        ambiguous=ambiguous,
    )


def _failed(
    error: dict[str, Any],
    *,
    kernel_fresh: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "console": "",
        "screenshots": [],
        "kernel_fresh": kernel_fresh,
    }


def _default_endpoint_payload() -> dict[str, Any]:
    from vibe.internal_client import (
        computer_script_endpoint_payload,
        resolve_computer_script_endpoint,
    )

    return computer_script_endpoint_payload(resolve_computer_script_endpoint())


def _default_node() -> Path | None:
    from vibe.desktop_runtime import private_desktop_node_bin

    return private_desktop_node_bin()


def _default_status() -> Any:
    from core.computer_use import effective_computer_use_status

    return effective_computer_use_status()


@dataclass
class _Kernel:
    process: asyncio.subprocess.Process
    writer: asyncio.StreamWriter
    artifact_directory: Path
    ready: asyncio.Future[None]
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    reader_task: asyncio.Task[None] | None = None
    alive: bool = True


@dataclass
class _Slot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kernel: _Kernel | None = None
    idle_handle: asyncio.TimerHandle | None = None


class ComputerJsKernelManager:
    """Own one persistent JavaScript kernel per Avibe Session."""

    def __init__(
        self,
        script_sessions: "ComputerScriptSessionManager",
        *,
        node_resolver: Callable[[], Path | None] = _default_node,
        endpoint_resolver: Callable[[], dict[str, Any]] = _default_endpoint_payload,
        status_reader: Callable[[], Any] = _default_status,
        idle_seconds: float = IDLE_SECONDS,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
        status_poll_seconds: float = STATUS_POLL_SECONDS,
    ) -> None:
        self._script_sessions = script_sessions
        self._node_resolver = node_resolver
        self._endpoint_resolver = endpoint_resolver
        self._status_reader = status_reader
        self._idle_seconds = idle_seconds
        self._kill_grace_seconds = kill_grace_seconds
        self._status_poll_seconds = status_poll_seconds
        self._slots: dict[str, _Slot] = {}
        self._in_flight: dict[str, asyncio.Future[str]] = {}
        self._call_ids = itertools.count(1)
        self._status_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()

    # Public surface -----------------------------------------------------

    async def evaluate(
        self,
        caller_session_id: str,
        code: Any,
        timeout_ms: Any,
    ) -> dict[str, Any]:
        """Run one call in the caller Session's kernel and return its report."""

        if not isinstance(code, str) or not code.strip():
            return _failed(_envelope("invalid_computer_js_code", "Code must be non-empty text."))
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return _failed(
                _envelope(
                    "computer_js_code_too_large",
                    "The code exceeded the size limit.",
                    params={"limit_bytes": MAX_CODE_BYTES},
                )
            )
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= MAX_TIMEOUT_MS
        ):
            return _failed(
                _envelope(
                    "invalid_timeout",
                    "The timeout is outside the supported range.",
                    params={"max_ms": MAX_TIMEOUT_MS},
                )
            )

        slot = self._slots.setdefault(caller_session_id, _Slot())
        async with slot.lock:
            self._cancel_idle(slot)
            try:
                return await self._evaluate_locked(
                    caller_session_id,
                    slot,
                    code,
                    timeout_ms,
                )
            finally:
                self._schedule_idle(caller_session_id, slot)

    def reset(self, caller_session_id: str) -> dict[str, Any]:
        """Discard the caller Session's kernel and every binding in it."""

        from core.computer_script_sessions import ComputerScriptSessionError

        try:
            session_id = self._script_sessions.check_caller(caller_session_id)
        except ComputerScriptSessionError as exc:
            return {"ok": False, "error": _envelope(exc.code, exc.code)}
        slot = self._slots.get(session_id)
        discarded = bool(slot and slot.kernel and slot.kernel.alive)
        if slot is not None:
            self._kill_slot(slot, "reset")
        return {"ok": True, "discarded": discarded}

    def on_token_invalidated(self, token: str, code: str) -> None:
        """Cancel the call bound to a script connection that just ended."""

        cancel = self._in_flight.get(token)
        if cancel is not None and not cancel.done():
            cancel.set_result(code)

    def discard_session(self, session_id: str) -> None:
        """Kill a Session's kernel when the Session is archived."""

        slot = self._slots.pop(session_id, None)
        if slot is not None:
            self._kill_slot(slot, "session_archived")

    def close_all(self) -> None:
        """Kill every kernel on service shutdown."""

        for slot in list(self._slots.values()):
            self._kill_slot(slot, "shutdown")
        self._slots.clear()
        if self._status_task is not None:
            self._status_task.cancel()
            self._status_task = None

    # Calls --------------------------------------------------------------

    async def _evaluate_locked(
        self,
        session_id: str,
        slot: _Slot,
        code: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        from core.computer_script_sessions import ComputerScriptSessionError

        status = await self._computer_use_status()
        if getattr(status, "status", None) == "off":
            self._kill_slot(slot, "computer_use_off")
            return _failed(
                _envelope(
                    "computer_use_unavailable",
                    "Computer Use is off.",
                    params={"status": "off", "reason": getattr(status, "reason", None)},
                )
            )
        try:
            opened = self._script_sessions.open(
                session_id,
                timeout_ms / 1000 + _BINDING_GRACE_SECONDS,
            )
        except ComputerScriptSessionError as exc:
            return _failed(_envelope(exc.code, exc.code))

        token = opened["connection_token"]
        # Registered before the kernel starts, so a Turn that ends while the
        # kernel is starting still cancels this call.
        cancel: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._in_flight[token] = cancel
        try:
            kernel_fresh = False
            kernel = slot.kernel
            if kernel is None or not kernel.alive:
                try:
                    kernel = await self._spawn()
                except _KernelGone as exc:
                    return _failed(
                        _envelope(
                            "js_kernel_unavailable",
                            "The JavaScript kernel could not be started.",
                            params={"reason": exc.reason},
                        )
                    )
                slot.kernel = kernel
                kernel_fresh = True
            if cancel.done():
                return _failed(_envelope(cancel.result(), cancel.result()), kernel_fresh=kernel_fresh)
            report = await self._run_call(slot, kernel, token, cancel, code, timeout_ms)
            return {**report, "kernel_fresh": kernel_fresh}
        finally:
            self._in_flight.pop(token, None)
            self._script_sessions.close(token)

    async def _run_call(
        self,
        slot: _Slot,
        kernel: _Kernel,
        token: str,
        cancel: asyncio.Future[str],
        code: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        call_id = next(self._call_ids)
        settled: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        kernel.pending[call_id] = settled
        try:
            await self._send(
                kernel,
                {
                    "type": "eval",
                    "id": call_id,
                    "code": code,
                    "connection_token": token,
                },
            )
            await asyncio.wait(
                {settled, cancel},
                timeout=timeout_ms / 1000,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not settled.done():
                reason = cancel.result() if cancel.done() else "js_timeout"
                await self._send(kernel, {"type": "cancel", "id": call_id, "code": reason})
                try:
                    await asyncio.wait_for(
                        asyncio.shield(settled),
                        self._kill_grace_seconds,
                    )
                except asyncio.TimeoutError:
                    self._kill_slot(slot, "unsettled")
            return self._report(settled.result())
        except _KernelGone as exc:
            return self._reset_report(exc.reason)
        except asyncio.CancelledError:
            # The CLI request went away; never leave the kernel busy with a
            # call nobody will collect.
            if kernel.alive:
                try:
                    await self._send(kernel, {"type": "cancel", "id": call_id})
                except _KernelGone:
                    pass
            raise
        finally:
            kernel.pending.pop(call_id, None)

    @staticmethod
    def _report(message: dict[str, Any]) -> dict[str, Any]:
        console = message.get("console")
        screenshots = message.get("screenshots")
        report: dict[str, Any] = {
            "ok": message.get("ok") is True,
            "console": console if isinstance(console, str) else "",
            "screenshots": [
                item for item in screenshots if isinstance(item, str)
            ]
            if isinstance(screenshots, list)
            else [],
        }
        if report["ok"]:
            report["result"] = message.get("result")
            if isinstance(message.get("result_text"), str):
                report["result_text"] = message["result_text"]
        else:
            error = message.get("error")
            report["error"] = (
                error
                if isinstance(error, dict)
                else _envelope(
                    "computer_script_protocol_error",
                    "The kernel returned an invalid result.",
                    ambiguous=True,
                )
            )
        return report

    @staticmethod
    def _reset_report(reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": _envelope(
                "js_kernel_reset",
                _RESET_MESSAGE,
                name="KernelReset",
                params={"reason": reason},
                ambiguous=True,
            ),
            "console": "",
            "screenshots": [],
        }

    # Kernel process -----------------------------------------------------

    async def _spawn(self) -> _Kernel:
        node = self._node_resolver()
        kernel_script = _sdk_directory() / "kernel.mjs"
        if node is None or not kernel_script.is_file():
            raise _KernelGone("runtime_unavailable")
        try:
            endpoint = await asyncio.to_thread(self._endpoint_resolver)
        except Exception as exc:
            raise _KernelGone("controller_unavailable") from exc

        artifact_directory = Path(tempfile.mkdtemp(prefix=_ARTIFACT_PREFIX)).resolve()
        os.chmod(artifact_directory, 0o700)
        (artifact_directory / ".active").touch(mode=0o600)
        parent, child = socket.socketpair()
        environment = {
            key: os.environ[key] for key in _KERNEL_ENV_KEYS if key in os.environ
        }
        environment[_KERNEL_FD_ENV] = str(child.fileno())
        try:
            process = await asyncio.create_subprocess_exec(
                str(node),
                str(kernel_script),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(child.fileno(),),
                start_new_session=True,
                env=environment,
                cwd=str(artifact_directory),
            )
        except OSError as exc:
            parent.close()
            self._release_artifacts(artifact_directory)
            raise _KernelGone("spawn_failed") from exc
        finally:
            child.close()

        reader, writer = await asyncio.open_connection(
            sock=parent,
            limit=_MESSAGE_LIMIT_BYTES,
        )
        kernel = _Kernel(
            process=process,
            writer=writer,
            artifact_directory=artifact_directory,
            ready=asyncio.get_running_loop().create_future(),
        )
        kernel.reader_task = asyncio.create_task(self._read(kernel, reader))
        try:
            await self._send(
                kernel,
                {
                    "type": "init",
                    "endpoint": endpoint,
                    "artifact_directory": str(artifact_directory),
                },
            )
            await asyncio.wait_for(asyncio.shield(kernel.ready), SPAWN_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, _KernelGone) as exc:
            self._kill(kernel, "spawn_failed")
            raise _KernelGone("spawn_failed") from exc
        self._ensure_status_watch()
        return kernel

    async def _read(self, kernel: _Kernel, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("type") == "ready":
                    if not kernel.ready.done():
                        kernel.ready.set_result(None)
                elif message.get("type") == "result":
                    settled = kernel.pending.get(message.get("id"))
                    if settled is not None and not settled.done():
                        settled.set_result(message)
        except (ValueError, OSError, asyncio.LimitOverrunError):
            logger.warning("Computer Use JavaScript kernel sent an invalid message")
        finally:
            self._kill(kernel, "exited")

    async def _send(self, kernel: _Kernel, message: dict[str, Any]) -> None:
        if not kernel.alive:
            raise _KernelGone("exited")
        try:
            kernel.writer.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
            await kernel.writer.drain()
        except (OSError, RuntimeError) as exc:
            self._kill(kernel, "exited")
            raise _KernelGone("exited") from exc

    def _kill(self, kernel: _Kernel, reason: str) -> None:
        if not kernel.alive:
            return
        kernel.alive = False
        try:
            os.killpg(kernel.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        kernel.writer.close()
        gone = _KernelGone(reason)
        if not kernel.ready.done():
            kernel.ready.set_exception(gone)
            kernel.ready.exception()
        for settled in kernel.pending.values():
            if not settled.done():
                settled.set_exception(gone)
        reaper = asyncio.get_running_loop().create_task(self._reap(kernel))
        self._background.add(reaper)
        reaper.add_done_callback(self._background.discard)

    async def _reap(self, kernel: _Kernel) -> None:
        try:
            await asyncio.wait_for(kernel.process.wait(), 5.0)
        except asyncio.TimeoutError:
            logger.warning("Computer Use JavaScript kernel %s was not reaped", kernel.process.pid)
        if kernel.reader_task is not None and kernel.reader_task is not asyncio.current_task():
            kernel.reader_task.cancel()
        self._release_artifacts(kernel.artifact_directory)

    @staticmethod
    def _release_artifacts(directory: Path) -> None:
        # Screenshots stay readable after the kernel ends; only the newest
        # inactive kernel directories are retained.
        (directory / ".active").unlink(missing_ok=True)
        _prune_retained_artifact_runs(directory.parent, _ARTIFACT_PREFIX)

    def _kill_slot(self, slot: _Slot, reason: str) -> None:
        self._cancel_idle(slot)
        if slot.kernel is not None:
            self._kill(slot.kernel, reason)
            slot.kernel = None

    # Idle and Computer Use off ------------------------------------------

    def _cancel_idle(self, slot: _Slot) -> None:
        if slot.idle_handle is not None:
            slot.idle_handle.cancel()
            slot.idle_handle = None

    def _schedule_idle(self, session_id: str, slot: _Slot) -> None:
        self._cancel_idle(slot)
        if slot.kernel is None or not slot.kernel.alive:
            return

        def expire() -> None:
            slot.idle_handle = None
            # A queued call re-arms the timer when it finishes.
            if not slot.lock.locked():
                self._kill_slot(slot, "idle")

        slot.idle_handle = asyncio.get_running_loop().call_later(
            self._idle_seconds,
            expire,
        )

    async def _computer_use_status(self) -> Any:
        try:
            return await asyncio.to_thread(self._status_reader)
        except Exception:
            logger.warning("Computer Use status read failed", exc_info=True)
            return None

    def _ensure_status_watch(self) -> None:
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.get_running_loop().create_task(
                self._watch_status()
            )

    async def _watch_status(self) -> None:
        while any(
            slot.kernel is not None and slot.kernel.alive
            for slot in self._slots.values()
        ):
            await asyncio.sleep(self._status_poll_seconds)
            status = await self._computer_use_status()
            if getattr(status, "status", None) == "off":
                for slot in self._slots.values():
                    if not slot.lock.locked():
                        self._kill_slot(slot, "computer_use_off")
