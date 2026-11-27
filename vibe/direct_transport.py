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
from vibe.direct_rendezvous import (
    DIRECT_ALPN,
    PUBLIC_TRIAL,
    PUBLIC_TRIAL_RELAYS,
    DirectEndpoint,
    DirectRendezvous,
    RENDEZVOUS_TTL_SECONDS,
    is_endpoint_id,
)

logger = logging.getLogger(__name__)

DIRECT_TRANSPORT_PROTOCOL = 1
CONTROL_LINE_LIMIT_BYTES = 4 * 1024
DATA_HEADER_LIMIT_BYTES = 160
DATA_HEADER_TIMEOUT_SECONDS = 2.0
MAX_CONNECTIONS = 64
MAX_STREAMS_PER_CONNECTION = 64
STOP_GRACE_SECONDS = 3.0
CONTROL_WRITE_TIMEOUT_SECONDS = 1.0
RESTART_INITIAL_DELAY_SECONDS = 0.25
RESTART_MAX_DELAY_SECONDS = 5.0

_HEX64_RE = re.compile(rb"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


@dataclass(frozen=True, slots=True)
class DirectTransportDiagnostics:
    """Safe transport state for status surfaces.

    ``connections`` is the monotonic count of valid loopback stream headers
    handed to ingress during this supervisor lifetime. ``refusals`` counts
    loopback data connections rejected before ingress. Neither field contains
    a peer label or address.
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


class _StreamLogBudget:
    """Loop-owned minute window. A stream keeps its opening sampling decision."""

    def __init__(self):
        self.timer: asyncio.TimerHandle | None = None
        self.sampled = self.opened = self.closed = 0
        self.bytes_in = self.bytes_out = self.max_ms = 0

    def _arm(self):
        if self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(60, self.flush)

    def open(self, ordinal):
        self._arm()
        if self.sampled < 20:
            self.sampled += 1
            logger.info("direct stream open stream=%d", ordinal)
            return True
        self.opened += 1
        return False

    def close(self, sampled, ordinal, duration_ms, bytes_in, bytes_out):
        if sampled:
            logger.info("direct stream close stream=%d duration_ms=%d bytes_in=%d bytes_out=%d",
                        ordinal, duration_ms, bytes_in, bytes_out)
            return
        self._arm()  # May close in a later window than it opened.
        self.closed += 1
        self.bytes_in += bytes_in
        self.bytes_out += bytes_out
        self.max_ms = max(self.max_ms, duration_ms)

    def flush(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.opened or self.closed:
            logger.info("direct stream summary opened=%d closed=%d bytes_in=%d bytes_out=%d max_duration_ms=%d",
                        self.opened, self.closed, self.bytes_in, self.bytes_out, self.max_ms)
        self.sampled = self.opened = self.closed = 0
        self.bytes_in = self.bytes_out = self.max_ms = 0


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
        relay_policy: str | None = None,
        stop_grace_seconds: float = STOP_GRACE_SECONDS,
        restart_initial_delay_seconds: float = RESTART_INITIAL_DELAY_SECONDS,
        restart_max_delay_seconds: float = RESTART_MAX_DELAY_SECONDS,
    ) -> None:
        if relay_policy not in (None, PUBLIC_TRIAL):
            raise ValueError("unsupported relay policy")
        self._relay_policy = relay_policy
        self._relay_origin: str | None = None
        self._published_addresses: tuple[str, ...] = ()
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
        self._control_write_lock: asyncio.Lock | None = None
        self._admission_notice_tasks: set[asyncio.Task[None]] = set()
        self._admission_listener_generation = 0
        self._active_admission_generation: int | None = None
        self._missing_executable_logged = False
        self._stream_port: int | None = None

        # These are intentionally private: neither token nor a peer address is
        # part of the diagnostic surface.
        self._token: bytes | None = None
        self._endpoint_id: str | None = None

        self._running = False
        self._reason: str | None = "not_running"
        self._stream_log = _StreamLogBudget()
        self._report_window = 0.0
        self._report_lines = self._report_skipped = 0
        self._report_pairs: set[int] = set()
        self._path_reports = False
        self._device_reports = False
        self._connections = 0
        self._refusals = 0
        self._started = False

    @property
    def running(self) -> bool:
        """Whether a sidecar generation has spawned; ``endpoint_id`` marks publication."""

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
        self._control_write_lock = asyncio.Lock()
        self._started = True
        self._running = False
        self._reason = "starting"
        self._endpoint_id = None
        self._active_admission_generation = None
        self._missing_executable_logged = False
        self._rendezvous.set_admission_listener(None)
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
            assert self._stop_event is not None
            self._stop_event.set()
            await self._close_stream_listener()
            task = self._supervisor_task
            if task is not None:
                await task
            self._supervisor_task = None
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
            self._connections += 1
            stream_id = self._connections
            stream = AsyncioByteStream(reader, writer)
            loop = asyncio.get_running_loop()
            started = loop.time()
            sampled = self._stream_log.open(stream_id)
            try:
                await self._ingress.handle_stream(stream, peer=peer)
            finally:
                self._stream_log.close(sampled, stream_id, int((loop.time() - started) * 1000),
                                       stream.bytes_read, stream.bytes_written)
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
        next_warning = float("-inf")
        suppressed = 0
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
                    if not self._missing_executable_logged:
                        logger.warning("direct transport sidecar unavailable reason=executable_not_found exit_status=unknown")
                        self._missing_executable_logged = True
                except (BrokenPipeError, ConnectionError, OSError):
                    reason = "control_channel_failed"
                except Exception:
                    reason = "supervisor_error"

                if self._stop_event.is_set():
                    final_reason = "stopped"
                    break
                final_reason = reason
                self._running = False
                self._withdraw(reason)
                await self._stop_process(process)
                if not self._stop_event.is_set() and reason != "executable_not_found":
                    now = asyncio.get_running_loop().time()
                    if now >= next_warning:
                        # Fatal suffixes are child-controlled; other reasons here are local constants.
                        code = "sidecar_fatal" if reason.startswith("fatal:") else reason
                        status = process.returncode if process is not None else None
                        logger.warning("direct transport sidecar restarting reason=%s exit_status=%s suppressed=%d",
                                       code, status if status is not None else "unknown", suppressed)
                        next_warning, suppressed = now + 60, 0
                    else:
                        suppressed += 1
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
                **({"relay": {"policy": self._relay_policy}} if self._relay_policy else {}),
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
                self._report_pairs.clear()
                self._device_reports = message.get("path_reports_v2") is True
                self._path_reports = self._device_reports or message.get("path_reports_v1") is True
                if self._path_reports:
                    await self._write_control(process.stdin, {"type": "observe_paths_v2" if self._device_reports else "observe_paths_v1"})
            elif message_type == "relay_status":
                self._handle_relay_status(message)
            elif message_type == "addresses":
                self._handle_addresses(message)
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
        self._relay_origin = None
        self._published_addresses = tuple(addresses)
        self._admission_listener_generation += 1
        generation = self._admission_listener_generation
        self._active_admission_generation = generation
        self._rendezvous.set_admission_listener(
            lambda client_endpoint_id, ttl_seconds, epoch, generation=generation: self._admission_notice_from_thread(
                generation,
                client_endpoint_id,
                ttl_seconds,
                epoch,
            )
        )
        try:
            self._rendezvous.publish(
                DirectEndpoint(endpoint_id, self._published_addresses, relay_policy=self._relay_policy),
                reason="running",
            )
        except BaseException:
            self._rendezvous.set_admission_listener(None)
            self._active_admission_generation = None
            raise

    def _handle_addresses(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is None:
            raise _ProtocolError("addresses_before_ready")
        self._published_addresses = tuple(self._addresses(message))
        self._publish_observation()

    def _handle_relay_status(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is None or self._relay_policy != PUBLIC_TRIAL or "url" not in message:
            raise _ProtocolError("unexpected_relay_status")
        url = message["url"]
        if url is not None and (not isinstance(url, str) or url not in PUBLIC_TRIAL_RELAYS):
            raise _ProtocolError("invalid_relay_url")
        self._relay_origin = url
        self._publish_observation()

    def _publish_observation(self) -> None:
        assert self._endpoint_id is not None
        self._rendezvous.publish(
            DirectEndpoint(self._endpoint_id, self._published_addresses, self._relay_origin, self._relay_policy),
            reason="running",
        )

    def _handle_path_report(self, msg: dict[str, Any]) -> None:
        fields = ("open", "rx_bytes", "tx_bytes", "challenge_tx", "challenge_rx", "response_tx", "response_rx")
        def uint(value):
            return type(value) is int and 0 <= value < 2**64
        rows = msg.get("families")
        schema = {"type", "ordinal", "phase", "selected", "partial", "families"}
        if self._device_reports:
            schema.update(("peer", "epoch", "scope", "rtt_ms"))
        if (not self._path_reports or self._endpoint_id is None or set(msg) != schema or
                msg["phase"] not in ("open", "change", "close") or
                msg["selected"] not in ("relay", "ipv4", "ipv6", "unknown") or
                not uint(msg["ordinal"]) or type(msg["partial"]) is not bool or
                not isinstance(rows, list) or len(rows) != 4 or
                any(not isinstance(row, dict) or set(row) != set(fields) or
                    not all(uint(row[k]) for k in fields) for row in rows)):
            raise _ProtocolError("invalid_path_report")
        if self._device_reports:
            selected, scope, rtt = msg["selected"], msg["scope"], msg["rtt_ms"]
            ip = selected in ("ipv4", "ipv6")
            if (not is_endpoint_id(msg["peer"]) or not uint(msg["epoch"]) or (rtt is not None and not uint(rtt))
                    or (scope not in ("local", "global") if ip else scope is not None)):
                raise _ProtocolError("invalid_path_report")
            path = None if selected == "unknown" else {
                "kind": ("lan" if scope == "local" else "punched") if ip else "relay",
                "family": selected if ip else None, "rtt_ms": rtt,
            }
            self._rendezvous.observe_path(msg["peer"], msg["epoch"], msg["ordinal"], msg["phase"], path)
        now = asyncio.get_running_loop().time()
        if now >= self._report_window:
            self._report_window = now + 60
            self._report_lines = 0
        ordinal, phase = msg["ordinal"], msg["phase"]
        free = 60 - self._report_lines - len(self._report_pairs)
        if phase == "open" and ordinal not in self._report_pairs and free >= 2:
            self._report_pairs.add(ordinal)
        elif phase == "close" and ordinal in self._report_pairs:
            self._report_pairs.remove(ordinal)
        elif phase == "change" and ordinal in self._report_pairs and free >= 1:
            pass
        else:
            self._report_skipped += 1
            return
        self._report_lines += 1
        # The class of the selected path's address, as the sidecar classed it:
        # `local` (private, shared address space, unique-local, link-local)
        # or `global`; `none` through the relay, for an unknown path, and
        # from a sidecar that does not say. Never the address.
        scope = msg.get("scope") if self._device_reports else None
        # Construct the logged values exclusively from the fixed schema above.
        logger.info("direct path connection=%d phase=%s selected=%s scope=%s partial=%s suppressed=%d %s",
                    msg["ordinal"], msg["phase"], msg["selected"], scope or "none", msg["partial"],
                    self._report_skipped,
                    " ".join(f"{family}.{k}={row[k]}" for family, row in
                             zip(("relay", "ipv4", "ipv6", "unknown"), rows) for k in fields))
        self._report_skipped = 0

    def _handle_peer_closed(self, message: dict[str, Any]) -> None:
        if self._endpoint_id is None:
            raise _ProtocolError("peer_closed_before_ready")
        endpoint_id = message.get("endpoint_id")
        reason = message.get("reason")
        if not is_endpoint_id(endpoint_id) or not isinstance(reason, str) or not reason:
            raise _ProtocolError("invalid_peer_closed")

    def _admission_notice_from_thread(self, generation: int, endpoint_id: str, ttl_seconds: int, epoch: int) -> None:
        if self._loop is None or not is_endpoint_id(endpoint_id):
            return
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            return
        try:
            self._loop.call_soon_threadsafe(
                self._queue_admission_notice,
                generation,
                endpoint_id,
                max(0, min(RENDEZVOUS_TTL_SECONDS, ttl_seconds)),
                epoch,
            )
        except RuntimeError:
            return

    def _queue_admission_notice(self, generation: int, endpoint_id: str, ttl_seconds: int, epoch: int) -> None:
        if (
            generation != self._active_admission_generation
            or self._stop_event is None
            or self._stop_event.is_set()
            or self._process is None
            or self._process.stdin is None
        ):
            return
        task = asyncio.create_task(
            self._send_admission_notice(self._process, generation, endpoint_id, ttl_seconds, epoch),
            name="direct-transport-admission-notice",
        )
        self._admission_notice_tasks.add(task)
        task.add_done_callback(self._admission_notice_tasks.discard)

    async def _send_admission_notice(
        self,
        process: Any,
        generation: int,
        endpoint_id: str,
        ttl_seconds: int,
        epoch: int,
    ) -> None:
        if generation != self._active_admission_generation or process is not self._process or process.stdin is None:
            return
        try:
            await self._write_control(
                process.stdin,
                {"type": "admission", "endpoint_id": endpoint_id, "ttl_seconds": ttl_seconds,
                 **({"epoch": epoch} if self._device_reports else {})},
            )
        except (_Stopping, BrokenPipeError, ConnectionError, OSError):
            return
        except Exception:
            logger.debug("direct transport admission notice failed", exc_info=True)

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
    def _addresses(message: dict[str, Any]) -> list[str]:
        addresses = message.get("addresses")
        if not isinstance(addresses, list) or any(not isinstance(address, str) for address in addresses):
            raise _ProtocolError("invalid_addresses")
        return addresses

    async def _write_control(
        self,
        stdin: asyncio.StreamWriter,
        message: dict[str, Any],
        *,
        interruptible: bool = True,
    ) -> None:
        lock = self._control_write_lock
        if lock is None:
            await self._write_control_unlocked(stdin, message, interruptible=interruptible)
            return
        async with lock:
            await self._write_control_unlocked(stdin, message, interruptible=interruptible)

    async def _write_control_unlocked(
        self,
        stdin: asyncio.StreamWriter,
        message: dict[str, Any],
        *,
        interruptible: bool,
    ) -> None:
        if message.get('type') == 'admission' and message['ttl_seconds'] > 0:
            # A queued grant may have waited on the loop or the control lock
            # across wake. Never deliver positive authority past key retirement.
            message = {**message, 'ttl_seconds': self._rendezvous.limit_admission_ttl(message['ttl_seconds'])}
        encoded = (json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > CONTROL_LINE_LIMIT_BYTES:
            raise _ProtocolError("control_line_too_large")
        stdin.write(encoded)
        if not interruptible or self._stop_event is None:
            await stdin.drain()
            return
        drain_task = asyncio.create_task(stdin.drain())
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, _pending = await asyncio.wait(
            (drain_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
            raise _Stopping
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await drain_task

    async def _drain_stderr(self, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        try:
            # Dedicated negotiated diagnostic sideband; never echo raw stderr.
            dropping = False
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError as exc:
                    await reader.readexactly(exc.consumed)
                    dropping = True
                    continue
                except asyncio.IncompleteReadError:
                    return
                if not dropping and self._path_reports and len(line) <= CONTROL_LINE_LIMIT_BYTES:
                    with contextlib.suppress(ValueError, UnicodeError, RecursionError, _ProtocolError):
                        message = json.loads(line)
                        if isinstance(message, dict) and message.get("type") == "path_report":
                            self._handle_path_report(message)
                dropping = False
        except (asyncio.CancelledError, ConnectionError, OSError):
            return
        finally:
            if self._device_reports:
                self._rendezvous.clear_paths()

    async def _drain_stdout(self, reader: asyncio.StreamReader | None) -> None:
        if reader is None:
            return
        try:
            while await reader.read(4096):
                pass
        except (asyncio.CancelledError, ConnectionError, OSError):
            return

    async def _stop_process(self, process: Any) -> None:
        if process is None:
            return
        stdout_task = asyncio.create_task(
            self._drain_stdout(process.stdout),
            name="direct-transport-sidecar-stdout",
        )
        wait_task = asyncio.create_task(process.wait(), name="direct-transport-sidecar-wait")
        try:
            if process.returncode is None:
                stdin = process.stdin
                if stdin is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._write_control(stdin, {"type": "stop"}, interruptible=False),
                            timeout=CONTROL_WRITE_TIMEOUT_SECONDS,
                        )
                    with contextlib.suppress(Exception):
                        stdin.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(stdin.wait_closed(), timeout=CONTROL_WRITE_TIMEOUT_SECONDS)
                done, _pending = await asyncio.wait(
                    (wait_task, stdout_task),
                    timeout=self._stop_grace_seconds,
                    return_when=asyncio.ALL_COMPLETED,
                )
                if wait_task not in done:
                    pid = process.pid
                    if pid is not None and process.returncode is None:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            if os.name == "nt":
                                process.kill()
                            else:
                                os.kill(pid, signal.SIGKILL)

            for task in (wait_task, stdout_task):
                if not task.done():
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(task), timeout=CONTROL_WRITE_TIMEOUT_SECONDS)
                if not task.done():
                    task.cancel()
            await asyncio.gather(wait_task, stdout_task, return_exceptions=True)
        finally:
            for task in (wait_task, stdout_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(wait_task, stdout_task, return_exceptions=True)

        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            if not stderr_task.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(stderr_task), timeout=CONTROL_WRITE_TIMEOUT_SECONDS)
            if not stderr_task.done():
                stderr_task.cancel()
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
        self._stream_log.flush()

    def _withdraw(self, reason: str) -> None:
        self._rendezvous.set_admission_listener(None)
        self._active_admission_generation = None
        for task in tuple(self._admission_notice_tasks):
            task.cancel()
        self._admission_notice_tasks.clear()
        self._endpoint_id = None
        self._reason = reason
        self._rendezvous.publish(None, reason=reason)


# A short name is useful to the later lifecycle owner without changing the
# explicit supervisor name used by tests and diagnostics callers.
DirectTransport = DirectTransportSupervisor

__all__ = [
    "CONTROL_LINE_LIMIT_BYTES",
    "CONTROL_WRITE_TIMEOUT_SECONDS",
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
