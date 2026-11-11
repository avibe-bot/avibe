"""Instance-side supervisor for the direct iroh transport.

The Web UI server owns this object together with :class:`DirectIngress` and
:class:`DirectRendezvous`.  It must be started, inspected, and stopped on that
server's asyncio event loop.  Process I/O and loopback sockets use asyncio
streams; this module never performs a blocking subprocess, socket, or process
operation on the loop.

The native sidecar is deliberately kept behind two small boundaries:

* a newline-delimited JSON control pipe, and
* a loopback TCP listener whose first line authenticates the sidecar and labels
  the transport peer before every accepted stream enters ``DirectIngress``.

This module is not imported by the product yet.  I3 owns the setting and
lifecycle wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
from dataclasses import dataclass
from os import PathLike
from typing import Any

from vibe.direct_ingress import AsyncioByteStream, DirectIngress
from vibe.direct_rendezvous import DIRECT_ALPN, DirectEndpoint, DirectRendezvous, is_endpoint_id

logger = logging.getLogger(__name__)

DIRECT_TRANSPORT_PROTOCOL = 1
CONTROL_LINE_LIMIT_BYTES = 4 * 1024
DATA_HEADER_LIMIT_BYTES = 160
DATA_HEADER_TIMEOUT_SECONDS = 2.0
MAX_CONNECTIONS = 64
MAX_STREAMS_PER_CONNECTION = 64
STOP_GRACE_SECONDS = 3.0
RESTART_INITIAL_DELAY_SECONDS = 0.25
RESTART_MAX_DELAY_SECONDS = 5.0

_HEX64_RE = re.compile(rb"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


@dataclass(frozen=True, slots=True)
class DirectTransportDiagnostics:
    """Safe transport state for status surfaces.

    ``connections`` is the monotonic count of sidecar peer connections that
    have been admitted at least once during this supervisor lifetime.  A
    repeated admission refresh does not increment it.  ``refusals`` counts
    denied admissions and loopback data connections rejected before ingress.
    Neither field contains a peer label or address.
    """

    running: bool
    reason: str | None
    endpoint_id: str | None
    connections: int
    refusals: int

    def as_dict(self) -> dict[str, Any]:
        """Return the same safe fields in a JSON-friendly shape."""

        return {
            "running": self.running,
            "reason": self.reason,
            "endpoint_id": self.endpoint_id,
            "connections": self.connections,
            "refusals": self.refusals,
        }


class DirectTransportError(RuntimeError):
    """The direct transport could not maintain its sidecar protocol."""


class _ProtocolError(DirectTransportError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Stopping(Exception):
    """Internal control-flow signal for a requested supervisor stop."""


class DirectTransportSupervisor:
    """Own the sidecar child and its loopback stream listener.

    ``executable`` is the already-installed sidecar path.  Binary resolution,
    installation, and lifecycle enablement are deliberately outside this
    class.  ``start`` creates one loopback listener and then supervises child
    generations until ``stop`` is called.
    """

    def __init__(
        self,
        executable: str | PathLike[str],
        ingress: DirectIngress,
        rendezvous: DirectRendezvous,
        *,
        stop_grace_seconds: float = STOP_GRACE_SECONDS,
        restart_initial_delay_seconds: float = RESTART_INITIAL_DELAY_SECONDS,
        restart_max_delay_seconds: float = RESTART_MAX_DELAY_SECONDS,
    ) -> None:
        self._executable = os.fspath(executable)
        self._ingress = ingress
        self._rendezvous = rendezvous
        self._stop_grace_seconds = max(0.0, stop_grace_seconds)
        self._restart_initial_delay = max(0.01, restart_initial_delay_seconds)
        self._restart_max_delay = max(self._restart_initial_delay, restart_max_delay_seconds)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._server: asyncio.AbstractServer | None = None
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._supervisor_task: asyncio.Task[None] | None = None
        self._process: Any = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stream_port: int | None = None

        # These are intentionally private: neither token nor a peer address is
        # part of the diagnostic surface.
        self._token: bytes | None = None
        self._endpoint_id: str | None = None
        self._admitted_peers: set[str] = set()

        self._running = False
        self._reason: str | None = "not_running"
        self._connections = 0
        self._refusals = 0
        self._started = False

    @property
    def running(self) -> bool:
        """Whether a sidecar generation is currently alive."""

        return self._running

    @property
    def diagnostics(self) -> DirectTransportDiagnostics:
        """Return transport state without exposing credentials or peer data."""

        return DirectTransportDiagnostics(
            running=self._running,
            reason=self._reason,
            endpoint_id=self._endpoint_id,
            connections=self._connections,
            refusals=self._refusals,
        )

    @property
    def status(self) -> DirectTransportDiagnostics:
        """Alias for callers that use ``status`` for read-only diagnostics."""

        return self.diagnostics

    async def start(self) -> None:
        """Bind the listener and begin sidecar supervision on this event loop."""

        self._assert_or_bind_loop()
        if self._started:
            return
        self._stop_event = asyncio.Event()
        self._started = True
        self._running = False
        self._reason = "starting"
        self._endpoint_id = None
        self._admitted_peers.clear()
        self._rendezvous.publish(None, reason="starting")
        try:
            self._server = await asyncio.start_server(
                self._spawn_stream_task,
                host="127.0.0.1",
                port=0,
                limit=DATA_HEADER_LIMIT_BYTES,
            )
            sockets = self._server.sockets or ()
            if not sockets:
                raise DirectTransportError("stream listener did not expose a socket")
            self._stream_port = int(sockets[0].getsockname()[1])
            self._supervisor_task = asyncio.create_task(
                self._supervise(),
                name="direct-transport-supervisor",
            )
            # Let the supervisor perform its first async spawn before returning
            # to a lifecycle caller, without waiting for sidecar readiness.
            await asyncio.sleep(0)
        except BaseException:
            await self._close_stream_listener()
            self._started = False
            self._running = False
            self._reason = "listener_start_failed"
            self._rendezvous.publish(None, reason=self._reason)
            raise

    async def stop(self) -> None:
        """Stop the child and listener, escalating only through its recorded PID."""

        self._assert_or_bind_loop()
        if not self._started:
            return
        assert self._stop_event is not None
        self._stop_event.set()
        await self._close_stream_listener()
        task = self._supervisor_task
        if task is not None:
            await task
        self._supervisor_task = None
        self._started = False
        self._running = False
        self._reason = "stopped"
        self._endpoint_id = None
        self._rendezvous.publish(None, reason="stopped")

    def _assert_or_bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("DirectTransportSupervisor must stay on its owning event loop")

    def _spawn_stream_task(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(
            self._handle_stream_connection(reader, writer),
            name="direct-transport-stream",
        )
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def _handle_stream_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        accepted = False
        try:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=DATA_HEADER_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, ValueError):
                self._refuse_stream()
                return
            if not self._valid_data_header(line):
                self._refuse_stream()
                return
            peer = self._data_header_peer(line)
            accepted = True
            await self._ingress.handle_stream(
                AsyncioByteStream(reader, writer),
                peer=peer,
            )
        except (ConnectionError, OSError):
            if not accepted:
                self._refuse_stream()
        except Exception:
            logger.exception("direct transport stream handling failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _valid_data_header(self, line: bytes) -> bool:
        if len(line) > DATA_HEADER_LIMIT_BYTES or not line.endswith(b"\n"):
            return False
        fields = line[:-1].split(b" ")
        if len(fields) != 3 or fields[0] != b"AVDT1":
            return False
        token = fields[1]
        expected = self._token.hex().encode("ascii") if self._token is not None else b""
        token_matches = hmac.compare_digest(token, expected)
        # Keep the constant-time comparison above on every structurally valid
        # three-field line, then reject non-canonical token bytes as malformed.
        return token_matches and _HEX64_RE.fullmatch(token) is not None and is_endpoint_id(
            self._decode_ascii(fields[2])
        )

    @staticmethod
    def _decode_ascii(value: bytes) -> str:
        try:
            return value.decode("ascii")
        except UnicodeDecodeError:
            return ""

    def _data_header_peer(self, line: bytes) -> str:
        return self._decode_ascii(line[:-1].split(b" ")[2])

    def _refuse_stream(self) -> None:
        self._refusals += 1

    async def _supervise(self) -> None:
        assert self._stop_event is not None
        next_delay = self._restart_initial_delay
        final_reason = "stopped"
        try:
            while not self._stop_event.is_set():
                process = None
                reason = "sidecar_exited"
                try:
                    process = await asyncio.create_subprocess_exec(
                        self._executable,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=CONTROL_LINE_LIMIT_BYTES,
                    )
                    self._process = process
                    self._stderr_task = asyncio.create_task(
                        self._drain_stderr(process.stderr),
                        name="direct-transport-sidecar-stderr",
                    )
                    self._token = secrets.token_bytes(32)
                    self._endpoint_id = None
                    self._admitted_peers.clear()
                    self._running = True
                    self._reason = None
                    await self._send_start(process)
                    reason = await self._read_control(process)
                except _Stopping:
                    final_reason = "stopped"
                    break
                except _ProtocolError as exc:
                    reason = exc.reason
                except FileNotFoundError:
                    reason = "executable_not_found"
                    logger.warning("direct transport sidecar executable is unavailable: %s", self._executable)
                except (BrokenPipeError, ConnectionError, OSError):
                    reason = "control_channel_failed"
                    logger.warning("direct transport sidecar control channel failed", exc_info=True)
                except Exception:
                    reason = "supervisor_error"
                    logger.exception("direct transport supervisor failed for one generation")

                if self._stop_event.is_set():
                    final_reason = "stopped"
                    break
                final_reason = reason
                self._running = False
                self._withdraw(reason)
                await self._stop_process(process)
                if self._process is process:
                    self._process = None
                self._token = None
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=next_delay)
                except asyncio.TimeoutError:
                    next_delay = min(self._restart_max_delay, max(next_delay * 2, self._restart_initial_delay))
        finally:
            self._running = False
            process = self._process
            if process is not None:
                with contextlib.suppress(Exception):
                    await self._stop_process(process)
                self._process = None
            self._token = None
            self._withdraw("stopped" if self._stop_event.is_set() else final_reason)

    async def _send_start(self, process: Any) -> None:
        if process.stdin is None or self._stream_port is None or self._token is None:
            raise DirectTransportError("sidecar control channel is incomplete")
        await self._write_control(
            process.stdin,
            {
                "type": "start",
                "protocol": DIRECT_TRANSPORT_PROTOCOL,
                "alpn": DIRECT_ALPN,
                "stream_addr": f"127.0.0.1:{self._stream_port}",
                "token": self._token.hex(),
                "max_connections": MAX_CONNECTIONS,
                "max_streams_per_connection": MAX_STREAMS_PER_CONNECTION,
            },
        )

    async def _read_control(self, process: Any) -> str:
        if process.stdout is None:
            raise _ProtocolError("missing_stdout")
        while True:
            try:
                line = await self._readline_or_stop(process.stdout)
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise _ProtocolError("oversized_line") from exc
            except asyncio.IncompleteReadError as exc:
                raise _ProtocolError("invalid_line") from exc
            if not line:
                return "sidecar_exited"
            if len(line) > CONTROL_LINE_LIMIT_BYTES or not line.endswith(b"\n"):
                raise _ProtocolError("invalid_line")
            message = self._decode_control_line(line)
            message_type = message.get("type")
            if message_type == "ready":
                self._handle_ready(message)
            elif message_type == "addresses":
                self._handle_addresses(message)
            elif message_type == "admit":
                await self._handle_admit(process, message)
            elif message_type == "peer_closed":
                self._handle_peer_closed(message)
            elif message_type == "fatal":
                return self._handle_fatal(message)
            else:
                raise _ProtocolError("unknown_message")

    async def _readline_or_stop(self, reader: asyncio.StreamReader) -> bytes:
        assert self._stop_event is not None
        read_task = asyncio.create_task(reader.readline())
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, _pending = await asyncio.wait(
            (read_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            raise _Stopping
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        return await read_task

    @staticmethod
    def _decode_control_line(line: bytes) -> dict[str, Any]:
        try:
            message = json.loads(line[:-1].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ProtocolError("invalid_line") from exc
        if not isinstance(message, dict):
            raise _ProtocolError("invalid_message")
        return message

    def _handle_ready(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is not None:
            raise _ProtocolError("duplicate_ready")
        if not self._is_protocol(message.get("protocol")):
            raise _ProtocolError("protocol_mismatch")
        endpoint_id = message.get("endpoint_id")
        addresses = self._addresses(message)
        if not is_endpoint_id(endpoint_id):
            raise _ProtocolError("invalid_endpoint_id")
        self._endpoint_id = endpoint_id
        self._rendezvous.publish(
            DirectEndpoint(endpoint_id=endpoint_id, addresses=tuple(addresses)),
            reason="running",
        )

    def _handle_addresses(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is None:
            raise _ProtocolError("addresses_before_ready")
        addresses = self._addresses(message)
        self._rendezvous.publish(
            DirectEndpoint(endpoint_id=self._endpoint_id, addresses=tuple(addresses)),
            reason="running",
        )

    async def _handle_admit(self, process: Any, message: dict[str, Any]) -> None:
        if self._endpoint_id is None:
            raise _ProtocolError("admit_before_ready")
        request = message.get("request")
        endpoint_id = message.get("endpoint_id")
        if not self._is_request_id(request) or not is_endpoint_id(endpoint_id):
            raise _ProtocolError("invalid_admit")
        allow = self._rendezvous.is_admitted(endpoint_id)
        if endpoint_id not in self._admitted_peers:
            self._admitted_peers.add(endpoint_id)
            self._connections += 1
        if not allow:
            self._refusals += 1
        if process.stdin is None:
            raise _ProtocolError("missing_stdin")
        await self._write_control(
            process.stdin,
            {"type": "admission", "request": request, "allow": allow},
        )

    def _handle_peer_closed(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is None:
            raise _ProtocolError("peer_closed_before_ready")
        endpoint_id = message.get("endpoint_id")
        reason = message.get("reason")
        if not is_endpoint_id(endpoint_id) or not isinstance(reason, str) or not reason:
            raise _ProtocolError("invalid_peer_closed")
        self._admitted_peers.discard(endpoint_id)

    @staticmethod
    def _handle_fatal(message: dict[str, Any]) -> str:
        error = message.get("error")
        if not isinstance(error, str) or _REASON_RE.fullmatch(error) is None:
            raise _ProtocolError("invalid_fatal")
        return f"fatal:{error}"

    @staticmethod
    def _is_protocol(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value == DIRECT_TRANSPORT_PROTOCOL

    @staticmethod
    def _is_request_id(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _addresses(message: dict[str, Any]) -> list[str]:
        addresses = message.get("addresses")
        if not isinstance(addresses, list) or any(not isinstance(address, str) for address in addresses):
            raise _ProtocolError("invalid_addresses")
        return addresses

    async def _write_control(self, stdin: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        encoded = (json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > CONTROL_LINE_LIMIT_BYTES:
            raise _ProtocolError("control_line_too_large")
        stdin.write(encoded)
        await stdin.drain()

    async def _drain_stderr(self, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        try:
            while line := await reader.readline():
                logger.debug("direct transport sidecar: %s", line.decode("utf-8", "replace").rstrip())
        except (asyncio.CancelledError, ConnectionError, OSError):
            return

    async def _stop_process(self, process: Any) -> None:
        if process is None:
            return
        if process.returncode is None:
            stdin = process.stdin
            if stdin is not None:
                with contextlib.suppress(Exception):
                    await self._write_control(stdin, {"type": "stop"})
                with contextlib.suppress(Exception):
                    stdin.close()
                with contextlib.suppress(Exception):
                    await stdin.wait_closed()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._stop_grace_seconds)
            except asyncio.TimeoutError:
                pid = process.pid
                if pid is not None and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        if os.name == "nt":
                            process.kill()
                        else:
                            os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await process.wait()
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            with contextlib.suppress(Exception):
                await stderr_task

    async def _close_stream_listener(self) -> None:
        server, self._server = self._server, None
        self._stream_port = None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = tuple(self._stream_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _withdraw(self, reason: str) -> None:
        self._endpoint_id = None
        self._admitted_peers.clear()
        self._reason = reason
        self._rendezvous.publish(None, reason=reason)


# A short name is useful to the later lifecycle owner without changing the
# explicit supervisor name used by tests and diagnostics callers.
DirectTransport = DirectTransportSupervisor

__all__ = [
    "CONTROL_LINE_LIMIT_BYTES",
    "DATA_HEADER_LIMIT_BYTES",
    "DATA_HEADER_TIMEOUT_SECONDS",
    "DIRECT_TRANSPORT_ALPN",
    "DIRECT_TRANSPORT_PROTOCOL",
    "DirectTransport",
    "DirectTransportDiagnostics",
    "DirectTransportError",
    "DirectTransportSupervisor",
    "MAX_CONNECTIONS",
    "MAX_STREAMS_PER_CONNECTION",
    "RESTART_INITIAL_DELAY_SECONDS",
    "RESTART_MAX_DELAY_SECONDS",
    "STOP_GRACE_SECONDS",
]

DIRECT_TRANSPORT_ALPN = DIRECT_ALPN
