#!/usr/bin/env python3
"""Test-only host of the v2 iOS/published-sidecar interoperability protocol.

No product imports, installed runtime, discovery, registration, or service state.
The caller supplies an already verified release binary in its isolated workspace.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

SCHEMA = 2
LINE_LIMIT = 4096
CASES = (
    "admitted", "not_admitted", "wrong_identity", "refused_connect",
    "admission_ends", "sidecar_stops",
)
NEGATIVE_CASES = {"not_admitted", "wrong_identity"}
TRIGGER_CASES = {"admission_ends", "sidecar_stops"}
ALPN = "avibe-direct/1"
HOSTNAME = "interop.invalid"
PAYLOAD = b"avibe-direct-interop-v2\n"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
WRONG_IDENTITY = "58" + "66" * 31
ENDPOINT = re.compile(r"[0-9a-f]{64}")


class HostFault(RuntimeError):
    """An observed host or wire-contract failure, never a native verdict."""


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise HostFault(detail)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON field: {key}")
        result[key] = value
    return result


async def read_message(reader: asyncio.StreamReader, timeout: float) -> dict:
    try:
        async with asyncio.timeout(timeout):
            line = await reader.readuntil(b"\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError) as error:
        raise HostFault(f"control read failed: {type(error).__name__}") from error
    require(len(line) <= LINE_LIMIT, "control line exceeds 4096 bytes including LF")
    try:
        value = json.loads(line, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(HostFault("non-finite JSON")))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise HostFault("invalid control JSON") from error
    require(isinstance(value, dict) and isinstance(value.get("type"), str), "control message must be a typed object")
    return value


async def send_message(writer: asyncio.StreamWriter, value: dict) -> None:
    line = json.dumps(value, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    require(len(line) <= LINE_LIMIT, "outgoing control line exceeds 4096 bytes")
    writer.write(line)
    async with asyncio.timeout(2):
        await writer.drain()


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(2):
            await writer.wait_closed()


async def finish_cleanup(task: asyncio.Task) -> None:
    """Join one owned cleanup even if its caller is cancelled repeatedly.

    Shielding alone is insufficient: it detaches cleanup when the caller exits.
    Keep joining the same strongly owned task, then propagate cancellation only
    after its bounded socket/TERM/KILL/reap sequence has actually completed.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


class Journal:
    """Host monotonic order only; no comparison to the App's clock."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record(self, event: str, **fields: Any) -> dict:
        require(len(self.events) < 4096, "host evidence event bound exceeded")
        entry = {"sequence": len(self.events), "monotonic_ns": time.monotonic_ns(),
                 "event": event, **fields}
        self.events.append(entry)
        return entry


class Sidecar:
    """One recorded child; drain both pipes and reap it on every exit path."""

    def __init__(self, binary: Path, directory: Path, journal: Journal) -> None:
        self.binary = binary
        self.directory = directory
        self.journal = journal
        self.process: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task] = []
        self.ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.ready_message: dict | None = None
        self.errors: list[str] = []
        self.stop_requested = False
        self.exit_event: dict | None = None
        self._close_task: asyncio.Task | None = None

    async def start(self, port: int, token: str) -> dict:
        self.process = await asyncio.create_subprocess_exec(
            str(self.binary), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=self.directory, limit=LINE_LIMIT,
        )
        self.journal.record("sidecar_spawn", pid=self.process.pid)
        write_json(self.directory / "pid.json", {"pid": self.process.pid, "executable": str(self.binary)})
        self.tasks = [asyncio.create_task(self._stdout()), asyncio.create_task(self._stderr()),
                      asyncio.create_task(self._wait())]
        await self.command({"type": "start", "protocol": 1, "alpn": ALPN,
                            "stream_addr": f"127.0.0.1:{port}", "token": token,
                            "max_connections": 8, "max_streams_per_connection": 8})
        async with asyncio.timeout(10):
            await self.ready.wait()
        self.check()
        require(self.ready_message is not None, "sidecar exited before ready")
        return self.ready_message

    async def command(self, value: dict) -> None:
        require(self.process is not None and self.process.stdin is not None, "sidecar not started")
        require(self.process.returncode is None, "sidecar exited before command")
        await send_message(self.process.stdin, value)

    def check(self) -> None:
        require(not self.errors, "; ".join(self.errors))
        if self.process is not None and self.process.returncode is not None:
            require(self.stop_requested and self.process.returncode == 0, "unexpected sidecar exit")

    async def _stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                # The process-lifetime timeout belongs to the enclosing case.
                message = await read_message(self.process.stdout, 60)
                kind = message["type"]
                require(kind in {"ready", "addresses", "peer_closed", "fatal"}, "unknown sidecar message")
                self.journal.record("sidecar_message", message=message)
                self.changed.set()
                if kind == "ready":
                    require(self.ready_message is None, "duplicate sidecar ready")
                    require(message.get("protocol") == 1 and isinstance(message.get("endpoint_id"), str)
                            and ENDPOINT.fullmatch(message["endpoint_id"]) is not None,
                            "invalid sidecar identity")
                    require(isinstance(message.get("addresses"), list) and message["addresses"], "sidecar has no addresses")
                    self.ready_message = message
                    self.ready.set()
                elif kind == "peer_closed":
                    require(isinstance(message.get("endpoint_id"), str)
                            and ENDPOINT.fullmatch(message["endpoint_id"]) is not None
                            and message.get("reason") in ("admission_ended", "idle_timeout", "stopped", "connection_closed"),
                            "invalid sidecar peer_closed")
                elif kind == "fatal":
                    raise HostFault(f"sidecar fatal: {message.get('error')}")
        except (HostFault, OSError) as error:
            # A clean pipe EOF is expected only after a requested stop. An
            # unterminated final line remains a fault, even during shutdown.
            cause = error.__cause__
            if not (self.stop_requested and isinstance(cause, asyncio.IncompleteReadError) and not cause.partial):
                self.errors.append(str(error))
            self.ready.set()

    async def _stderr(self) -> None:
        assert self.process and self.process.stderr
        size = 0
        with (self.directory / "sidecar.stderr").open("wb") as output:
            while data := await self.process.stderr.read(4096):
                size += len(data)
                if size <= 65536:
                    output.write(data)
                elif not self.errors:
                    self.errors.append("sidecar stderr bound exceeded")

    async def _wait(self) -> None:
        assert self.process
        code = await self.process.wait()
        self.exit_event = self.journal.record("sidecar_exit", pid=self.process.pid, code=code)
        if not self.stop_requested or code != 0:
            self.errors.append(f"unexpected sidecar exit: {code}")
        self.ready.set()

    async def stop(self) -> None:
        require(not self.stop_requested, "duplicate sidecar stop")
        self.stop_requested = True
        await self.command({"type": "stop"})

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            try:
                if not self.stop_requested:
                    await self.stop()
                async with asyncio.timeout(4):
                    await process.wait()
            except (OSError, HostFault, TimeoutError):
                self.errors.append("sidecar required forced cleanup")
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                try:
                    async with asyncio.timeout(2):
                        await process.wait()
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    async with asyncio.timeout(3):
                        await process.wait()
        if process.stdin:
            await close_writer(process.stdin)
        # Readers finish on EOF; do not cancel away malformed trailing output.
        try:
            async with asyncio.timeout(3):
                await asyncio.gather(*self.tasks)
        except TimeoutError:
            self.errors.append("sidecar pipe cleanup timed out")
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)


class Ingress:
    """Loopback test fixture: authenticate AVDT1, separate setup from case I/O."""

    def __init__(self, client_id: str, token: str, journal: Journal) -> None:
        self.client_id = client_id
        self.token = token
        self.journal = journal
        self.phase = "setup"
        self.answer = 200
        self.server: asyncio.Server | None = None
        self.closing = False
        self.tasks: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.streams: list[dict] = []
        self.errors: list[str] = []
        self._close_task: asyncio.Task | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=LINE_LIMIT)
        return self.server.sockets[0].getsockname()[1]

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self.streams) >= 64:
            self.errors.append("ingress stream bound exceeded")
            writer.close()
            return
        # Record at accept, including bad headers: negative cases require zero
        # ingress connections, not merely zero successfully authenticated ones.
        stream = {"index": len(self.streams), "phase": self.phase, "authenticated": False, "kind": None,
                  "received": 0, "sent": 0, "eof": False, "reset": False, "closed": False}
        self.streams.append(stream)
        self.journal.record("ingress_accept", stream=len(self.streams) - 1, phase=self.phase)
        if self.closing:
            self.errors.append("ingress connection arrived during cleanup")
            stream["closed"] = True
            writer.close()
            return
        self.writers.add(writer)
        task = asyncio.create_task(self._serve(reader, writer, stream))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stream: dict) -> None:
        try:
            async with asyncio.timeout(35):
                header = await reader.readuntil(b"\n")
                require(header == f"AVDT1 {self.token} {self.client_id}\n".encode(), "bad ingress AVDT1 header")
                stream["authenticated"] = True
                # Probe/payload handling is defined by the frozen v2 wire
                # contract, not by product ingress or production test hooks.
                first = await reader.readexactly(1)
                if first == b"C":
                    request = first + await reader.readuntil(b"\r\n\r\n")
                    expected = f"CONNECT {HOSTNAME}:443 HTTP/1.1\r\nHost: {HOSTNAME}:443\r\n\r\n".encode()
                    require(request == expected, "noncanonical probe CONNECT")
                    stream["kind"] = "probe"
                    stream["status"] = self.answer
                    writer.write(f"HTTP/1.1 {self.answer} {'Connection Established' if self.answer == 200 else 'Forbidden'}\r\n\r\n".encode())
                    await writer.drain()
                    # Probe cancellation may reset the stream; that is not a
                    # payload reset and never satisfies a payload FIN check.
                    extra = await reader.read(1)
                    require(not extra, "probe carried unexpected bytes")
                    stream["eof"] = True
                else:
                    require(self.phase == "case" and self.answer == 200, "payload outside an admitted case")
                    stream["kind"] = "payload"
                    data = first
                    digest = hashlib.sha256()
                    while data:
                        stream["received"] += len(data)
                        require(stream["received"] <= len(PAYLOAD), "payload bound exceeded")
                        digest.update(data)
                        stream["sha256"] = digest.hexdigest()
                        writer.write(data)
                        await writer.drain()
                        stream["sent"] += len(data)
                        data = await reader.read(8192)
                    stream["eof"] = True
                    writer.write_eof()
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            stream["reset"] = True
            if not stream["authenticated"]:
                self.errors.append("incomplete ingress authentication header")
        except (HostFault, asyncio.LimitOverrunError, TimeoutError, OSError) as error:
            self.errors.append(str(error))
        finally:
            stream["closed"] = True
            self.journal.record("ingress_close", stream=stream["index"],
                                eof=stream["eof"], reset=stream["reset"])
            await close_writer(writer)
            self.writers.discard(writer)

    async def finish_setup(self, refused: bool) -> None:
        # Wait for setup connections to retire before acknowledging the phase
        # boundary. A case cannot inherit a live setup stream.
        if self.tasks:
            async with asyncio.timeout(2):
                await asyncio.gather(*tuple(self.tasks))
        require(not self.errors, "; ".join(self.errors))
        require(self.streams and all(s["kind"] == "probe" and s.get("status") == 200
                                    for s in self.streams), "setup has no successful 200 probe")
        self.phase = "case"
        self.answer = 403 if refused else 200
        self.journal.record("setup_done", streams=len(self.streams))

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        self.closing = True
        if self.server:
            self.server.close()
        # A newly accepted task may be cancelled before its first instruction;
        # its coroutine finally block then never runs. Own writers separately.
        writers = tuple(self.writers)
        for writer in writers:
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_writer(writer) for writer in writers))
        self.writers.clear()
        if self.server:
            async with asyncio.timeout(3):
                await self.server.wait_closed()


def admissible_addresses(addresses: list[str]) -> list[str]:
    """Match the App's RFC1918/ULA boundary, not ipaddress.is_private's superset."""
    networks = tuple(ipaddress.ip_network(cidr) for cidr in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
    ))
    result = []
    for address in addresses:
        require(isinstance(address, str), "sidecar address is not a string")
        host, separator, port = address.rpartition(":")
        require(bool(separator) and port.isdigit() and 0 < int(port) <= 65535, "invalid sidecar address")
        try:
            ip = ipaddress.ip_address(host.strip("[]"))
        except ValueError as error:
            raise HostFault("sidecar did not offer an IP address") from error
        if any(ip in network for network in networks if ip.version == network.version):
            result.append(address)
    return result


class Case:
    def __init__(self, name: str, client_id: str, binary: Path, directory: Path) -> None:
        self.name = name
        self.client_id = client_id
        self.journal = Journal()
        self.started_ns = time.monotonic_ns()
        self.ingress = Ingress(client_id, secrets.token_hex(32), self.journal)
        self.sidecar = Sidecar(binary, directory, self.journal)
        self.trigger: dict | None = None
        self.armed: dict | None = None
        self.offer: dict = {}
        self.app_result: dict | None = None
        self._close_task: asyncio.Task | None = None

    async def start(self, tier: str) -> dict:
        port = await self.ingress.start()
        ready = await self.sidecar.start(port, self.ingress.token)
        require(ready["endpoint_id"] != self.client_id, "client reused sidecar identity")
        require(WRONG_IDENTITY not in {self.client_id, ready["endpoint_id"]}, "wrong-identity fixture collision")
        if self.name != "not_admitted":
            await self.sidecar.command({"type": "admission", "endpoint_id": self.client_id, "ttl_seconds": 300})
        if self.name in NEGATIVE_CASES:
            self.ingress.phase = "case"
        addresses = ready["addresses"]
        admissible = admissible_addresses(addresses)
        self.offer = {
            "type": "case", "schema": SCHEMA, "name": self.name,
            "sidecar_endpoint_id": WRONG_IDENTITY if self.name == "wrong_identity" else ready["endpoint_id"],
            "addresses": addresses if tier == "engine" else admissible,
            "admissible_addresses": admissible, "alpn": ALPN, "hostname": HOSTNAME,
            "instance_id": "interop-fixture", "generation": "interop-v2-1", "lifetime_seconds": 300,
        }
        return self.offer

    async def setup_done(self, writer: asyncio.StreamWriter) -> None:
        require(self.name not in NEGATIVE_CASES and self.ingress.phase == "setup", "unexpected setup_done")
        await self.ingress.finish_setup(self.name == "refused_connect")
        # This ACK is the phase barrier across the independent control and
        # ingress sockets. App must not start the case until it has read it.
        await send_message(writer, {"type": "setup_done", "schema": SCHEMA, "name": self.name})

    def payload(self) -> dict:
        streams = [s for s in self.ingress.streams if s["phase"] == "case" and s["kind"] == "payload"]
        require(len(streams) == 1, "expected exactly one payload stream")
        stream = streams[0]
        require(stream["received"] == stream["sent"] == len(PAYLOAD)
                and stream.get("sha256") == PAYLOAD_SHA256, "payload bytes/hash mismatch")
        return stream

    async def arm(self, writer: asyncio.StreamWriter) -> None:
        require(self.name in TRIGGER_CASES and self.armed is None, "unexpected or repeated armed")
        require(self.ingress.phase == "case", "armed before setup barrier")
        stream = self.payload()
        require(not stream["closed"] and not stream["eof"] and not stream["reset"], "payload not alive at armed")
        self.sidecar.check()
        self.armed = self.journal.record("armed")
        self.trigger = self.journal.record("trigger", action=self.name)
        if self.name == "admission_ends":
            await self.sidecar.command({"type": "admission", "endpoint_id": self.client_id, "ttl_seconds": 0})
        else:
            await self.sidecar.stop()
        await send_message(writer, {"type": "triggered", "schema": SCHEMA, "name": self.name})

    async def finish_io(self) -> None:
        # App has finished its observations. Drain the corresponding host EOF
        # and sidecar output before deciding; this is bounded, never a sleep.
        if self.name == "sidecar_stops" and self.sidecar.process:
            async with asyncio.timeout(4):
                await self.sidecar.process.wait()
                await asyncio.gather(*self.sidecar.tasks)
        if self.name == "admission_ends" and self.trigger:
            async with asyncio.timeout(2):
                while True:
                    self.sidecar.changed.clear()
                    if any(e["event"] == "sidecar_message" and e["sequence"] > self.trigger["sequence"]
                           and e["message"].get("type") == "peer_closed"
                           and e["message"].get("endpoint_id") == self.client_id
                           and e["message"].get("reason") == "admission_ended" for e in self.journal.events):
                        break
                    self.sidecar.check()
                    await self.sidecar.changed.wait()
        if self.ingress.tasks:
            async with asyncio.timeout(2):
                await asyncio.gather(*tuple(self.ingress.tasks))

    def host_observed(self) -> dict:
        setup = [s for s in self.ingress.streams if s["phase"] == "setup"]
        case = [s for s in self.ingress.streams if s["phase"] == "case"]
        payload = [s for s in case if s["kind"] == "payload"]
        reasons = [e for e in self.journal.events if e["event"] == "sidecar_message"
                   and e["message"].get("type") == "peer_closed"
                   and e["message"].get("endpoint_id") == self.client_id]
        observed = {
            "setup_streams": len(setup), "case_streams": len(case),
            "payload_sent_bytes": sum(s["sent"] for s in payload),
            "payload_received_bytes": sum(s["received"] for s in payload),
            "client_id_seen": bool(case) and all(s["authenticated"] for s in case),
            "fin_seen": bool(payload) and all(s["eof"] and not s["reset"] for s in payload),
        }
        if payload:
            observed["payload_sha256"] = payload[0].get("sha256")
        if reasons:
            observed["peer_closed_reason"] = reasons[-1]["message"]["reason"]
        for key, event in (("armed_ms", self.armed), ("trigger_ms", self.trigger)):
            if event:
                observed[key] = (event["monotonic_ns"] - self.started_ns) / 1_000_000
        if self.trigger:
            closed = (self.sidecar.exit_event if self.name == "sidecar_stops" else
                      next((e for e in reasons if e["sequence"] > self.trigger["sequence"]
                            and e["message"]["reason"] == "admission_ended"), None))
            if closed:
                observed["closed_ms"] = (closed["monotonic_ns"] - self.started_ns) / 1_000_000
            if self.name == "sidecar_stops" and self.sidecar.process:
                observed["child_exit"] = self.sidecar.process.returncode
        return observed

    def confirm(self, result: dict) -> dict:
        self.sidecar.check()
        require(not self.ingress.errors, "; ".join(self.ingress.errors))
        require(result.get("outcome") == "passed", "App did not pass the case")
        app = result.get("observed")
        require(isinstance(app, dict) and app.get("control_tcp") is True, "missing App observations")
        observed = self.host_observed()
        case = [s for s in self.ingress.streams if s["phase"] == "case"]
        if self.name in NEGATIVE_CASES:
            require(not self.ingress.streams, "negative case reached ingress")
            expected = {"handshake"} if self.name == "wrong_identity" else {"handshake", "closed_by_peer"}
            require(app.get("failure") in expected and app.get("case_streams") == 0,
                    "negative case did not observe peer/handshake rejection")
            require("setup_attempts" not in app, "negative case retried setup")
            # No invented peer_closed: the pre-handshake hook can emit none.
            return observed
        require(self.ingress.phase == "case", "missing setup phase barrier")
        require(type(app.get("setup_attempts")) is int and app["setup_attempts"] >= 1, "invalid setup attempts")
        setup_probe = app.get("setup_probe", {})
        require(isinstance(setup_probe, dict) and setup_probe.get("status") == 200
                and setup_probe.get("streams") == observed["setup_streams"],
                "App/host setup stream count mismatch")
        require(all(s["authenticated"] for s in case), "invalid ingress identity")
        probes = [s for s in case if s["kind"] == "probe"]
        require(len(probes) == 1, "expected exactly one case probe")
        status = 403 if self.name == "refused_connect" else 200
        case_probe = app.get("case_probe")
        require(isinstance(case_probe, dict) and probes[0].get("status") == status and case_probe.get("status") == status,
                "case probe response mismatch")
        if self.name == "refused_connect":
            require(len(case) == 1 and app.get("failure") == "probe_refused", "case was not refused exactly once")
            return observed
        require(len(case) == 2, "expected one case probe and one payload stream")
        stream = self.payload()
        require(app.get("dial") == "established" and app.get("remote_identity") == "matched", "App identity not matched")
        counts = app.get("payload", {})
        require(isinstance(counts, dict) and counts.get("sent_bytes") == counts.get("received_bytes") == len(PAYLOAD), "App/host byte counts disagree")
        if self.name == "admitted":
            require(observed["fin_seen"] and counts.get("sent_fin") is True and counts.get("received_fin") is True,
                    "payload did not complete with FIN in both directions")
        else:
            require(app.get("armed_alive") is True and app.get("closed_after_armed") is True, "App closure evidence missing")
            require(self.armed is not None and self.trigger is not None and "closed_ms" in observed, "host trigger/close evidence missing")
            require(stream["closed"] and any(e["event"] == "ingress_close" and e["stream"] == stream["index"]
                                            and e["sequence"] > self.trigger["sequence"] for e in self.journal.events),
                    "host payload stream did not close after trigger")
            require(observed["armed_ms"] <= observed["trigger_ms"] <= observed["closed_ms"], "host close preceded trigger")
            if self.name == "sidecar_stops":
                require(observed.get("child_exit") == 0, "sidecar did not exit cleanly")
        return observed

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        # Stop sidecar first so active forwarding is retired, then listeners.
        try:
            await self.sidecar.close()
        finally:
            await self.ingress.close()


class Host:
    """Own both tiers and persist only independent, identifier-free verdicts."""

    def __init__(self, binary: Path, directory: Path) -> None:
        self.binary = binary
        self.directory = directory
        self.results: dict[str, dict] = {}
        self.claimed_tiers: set[str] = set()
        self.client_ids: set[str] = set()
        self.errors: list[str] = []
        self.tasks: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.server: asyncio.Server | None = None
        self.closing = False
        self.owns_directory = False
        self.complete = asyncio.Event()
        self._close_task: asyncio.Task | None = None

    async def start(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        require(not any(self.directory.iterdir()), "interop result directory must be fresh")
        self.owns_directory = True
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=LINE_LIMIT)
        fixture = self.directory / "fixture.json"
        write_json(fixture, {"schema": SCHEMA, "control": f"127.0.0.1:{self.server.sockets[0].getsockname()[1]}",
                             "result_dir": str(self.directory), "case_deadline_seconds": 20,
                             "setup_deadline_seconds": 10})
        return fixture

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closing or len(self.tasks) >= 2:
            self.errors.append("unexpected extra control connection")
            writer.close()
            return
        self.writers.add(writer)
        task = asyncio.create_task(self._serve(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tier = None
        records = []
        try:
            hello = await read_message(reader, 10)
            tier = hello.get("tier")
            require(hello.get("type") == "hello" and hello.get("schema") == SCHEMA and isinstance(tier, str)
                    and tier in {"engine", "transport"} and tier not in self.claimed_tiers, "invalid/duplicate tier hello")
            self.claimed_tiers.add(tier)
            for name in CASES:
                request = await read_message(reader, 20)
                client_id = request.get("client_endpoint_id")
                require(request["type"] == "next" and isinstance(client_id, str)
                        and ENDPOINT.fullmatch(client_id) is not None and client_id not in self.client_ids,
                        "next requires a fresh endpoint ID")
                self.client_ids.add(client_id)
                directory = self.directory / tier / name
                directory.mkdir(parents=True)
                case = Case(name, client_id, self.binary, directory)
                failure = None
                skip = False
                try:
                    offer = await case.start(tier)
                    await send_message(writer, offer)
                    empty_transport = tier == "transport" and not offer["admissible_addresses"]
                    skip = empty_transport and name == "admitted" and not records
                    async with asyncio.timeout(20 if name in NEGATIVE_CASES or skip else 10) as deadline:
                        while True:
                            message = await read_message(reader, 20 if case.ingress.phase == "case" or skip else 10)
                            require(message.get("name") == name and message.get("schema") == SCHEMA, "case message identity/schema mismatch")
                            kind = message["type"]
                            if kind == "setup_done" and not skip:
                                await case.setup_done(writer)
                                deadline.reschedule(asyncio.get_running_loop().time() + 20)
                            elif kind == "armed" and not skip:
                                await case.arm(writer)
                            elif kind == "result":
                                case.app_result = message
                                require(not empty_transport or skip,
                                        "transport addresses disappeared after coverage began")
                                if skip:
                                    require(message.get("outcome") == "not_covered" and not case.ingress.streams,
                                            "empty-address transport tier did not visibly skip")
                                else:
                                    await case.finish_io()
                                    case.confirm(message)
                                break
                            else:
                                raise HostFault("unexpected case message")
                except (HostFault, OSError, TimeoutError) as error:
                    failure = str(error) or type(error).__name__
                finally:
                    await case.close()
                if case.sidecar.errors or case.ingress.errors:
                    failure = failure or "; ".join(case.sidecar.errors + case.ingress.errors)
                # The initial proof must hold before cleanup, so stopping the
                # fixture cannot manufacture closure evidence. Recheck after
                # cleanup too: late ingress must not disappear from a cached
                # verdict while the sidecar is still able to reach the listener.
                if failure is None:
                    try:
                        if skip:
                            require(not case.ingress.streams, "transport skip reached ingress during cleanup")
                        else:
                            case.confirm(case.app_result)
                    except HostFault as error:
                        failure = str(error)
                record = {"name": name, "host": "contradicted" if failure else "confirmed",
                          "observed": case.host_observed(), "detail": failure}
                if case.app_result:
                    app_record = {k: v for k, v in case.app_result.items() if k not in {"type", "schema"}}
                    record["app_result_sha256"] = hashlib.sha256(
                        json.dumps(app_record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                    ).hexdigest()
                records.append(record)
                await send_message(writer, {"type": "verdict", "schema": SCHEMA, "name": name,
                                            "host": record["host"], "detail": failure})
                if case.app_result is None:
                    raise HostFault(f"{name}: no App result; control cannot safely continue")
                if skip:
                    require(not failure, "transport skip was contradicted")
                    records = []
                    break
            await send_message(writer, {"type": "done", "schema": SCHEMA})
            self.results[tier] = {"schema": SCHEMA, "tier": tier,
                                  "status": "not_covered" if not records else
                                  ("passed" if all(r["host"] == "confirmed" for r in records) else "failed"),
                                  "cases": records}
        except (HostFault, OSError, TimeoutError) as error:
            self.errors.append(str(error) or type(error).__name__)
            if isinstance(tier, str) and tier in {"engine", "transport"} and tier not in self.results:
                self.results[tier] = {"schema": SCHEMA, "tier": tier, "status": "failed", "cases": records}
        finally:
            await close_writer(writer)
            self.writers.discard(writer)
            self.persist()
            if len(self.results) == 2:
                self.complete.set()

    def persist(self) -> None:
        if not self.owns_directory:
            return
        document = dict(self.results.get("engine", {"schema": SCHEMA, "tier": "engine", "status": "failed", "cases": []}))
        if "transport" in self.results:
            document["transport"] = self.results["transport"]
        document["errors"] = list(self.errors)
        if self.errors:
            document["status"] = "failed"
        write_json(self.directory / "host.json", document)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await finish_cleanup(self._close_task)

    async def _close(self) -> None:
        self.closing = True
        if self.server:
            self.server.close()
        for writer in tuple(self.writers):
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            async with asyncio.timeout(3):
                await self.server.wait_closed()
        self.persist()
