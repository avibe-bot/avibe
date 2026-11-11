"""Real-child tests for the instance side of the direct transport contract."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vibe import direct_transport
from vibe.direct_ingress import DirectIngress
from vibe.direct_rendezvous import DirectEndpoint, DirectRendezvous
from vibe.direct_transport import (
    DATA_HEADER_LIMIT_BYTES,
    DirectTransportSupervisor,
)

PEER = "a" * 64
ENDPOINT_ONE = "1" * 64
ENDPOINT_TWO = "2" * 64
HOSTNAME = "alex.avibe.bot"


SIDECAR = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    import json
    import os
    import socket
    import sys
    import time
    from pathlib import Path

    event_path = Path(os.environ["DIRECT_TEST_EVENTS"])
    mode = os.environ.get("DIRECT_TEST_MODE", "wait")

    def event(value):
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")

    def send(value):
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def wait_for_stop():
        while True:
            line = sys.stdin.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except Exception:
                continue
            if message.get("type") == "stop":
                return

    def next_generation():
        counter_path = Path(os.environ.get("DIRECT_TEST_GENERATIONS", str(event_path) + ".generations"))
        try:
            generation = int(counter_path.read_text(encoding="ascii")) + 1
        except (FileNotFoundError, ValueError):
            generation = 1
        counter_path.write_text(str(generation), encoding="ascii")
        return generation

    start_line = sys.stdin.readline()
    if not start_line:
        raise SystemExit(2)
    start = json.loads(start_line)
    event({"kind": "start", "pid": os.getpid(), "start": start})
    host, port_text = start["stream_addr"].rsplit(":", 1)
    port = int(port_text)
    generation = next_generation()
    peer = "a" * 64
    endpoint_one = "1" * 64
    endpoint_two = "2" * 64
    endpoint_id = (endpoint_one if generation % 2 else endpoint_two)
    addresses = ["192.168.1.20:51234"]

    def ready(protocol=1):
        send({"type": "ready", "protocol": protocol, "endpoint_id": endpoint_id, "addresses": addresses})

    def read_admission_notice():
        message = json.loads(sys.stdin.readline())
        event({"kind": "admission_notice", "message": message})

    if mode == "ready_addresses":
        ready()
        time.sleep(0.1)
        send({"type": "addresses", "addresses": ["192.168.1.21:51235"]})
        wait_for_stop()
    elif mode == "admission":
        ready()
        read_admission_notice()
        read_admission_notice()
        wait_for_stop()
    elif mode == "stream":
        ready()
        time.sleep(0.1)
        with socket.create_connection((host, port), timeout=3) as sock:
            header = f"AVDT1 {start['token']} {peer}\n".encode("ascii")
            sock.sendall(header + b"payload")
        wait_for_stop()
    elif mode == "real_ingress":
        ready()
        time.sleep(0.1)
        with socket.create_connection((host, port), timeout=3) as sock:
            head = (
                f"AVDT1 {start['token']} {peer}\n"
                f"CONNECT {os.environ['DIRECT_TEST_HOSTNAME']}:443 HTTP/1.1\r\n"
                f"Host: {os.environ['DIRECT_TEST_HOSTNAME']}:443\r\n\r\n"
            ).encode("ascii")
            sock.sendall(head)
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            event({"kind": "connect_response", "response": response.decode("latin-1")})
        wait_for_stop()
    elif mode == "bad_headers":
        header_limit = 160
        wrong = f"AVDT1 {'0' * 64} {peer}\n".encode("ascii")
        malformed = b"not-a-direct-header\n"
        oversized = b"x" * (header_limit + 1) + b"\n"
        incomplete = b"AVDT1 "
        for payload in (wrong, malformed, oversized, incomplete):
            try:
                with socket.create_connection((host, port), timeout=3) as sock:
                    sock.sendall(payload)
                    sock.settimeout(3)
                    try:
                        sock.recv(1)
                    except (socket.timeout, ConnectionResetError, OSError):
                        pass
            except OSError:
                pass
        wait_for_stop()
    elif mode == "fatal_restart":
        ready()
        if generation == 1:
            send({"type": "fatal", "error": "synthetic_failure"})
            wait_for_stop()
        else:
            wait_for_stop()
    elif mode == "restart":
        ready()
        if generation == 1:
            raise SystemExit(0)
        wait_for_stop()
    elif mode == "wrong_protocol":
        ready(protocol=2)
        wait_for_stop()
    elif mode == "unknown_message":
        ready()
        send({"type": "unknown"})
        wait_for_stop()
    elif mode == "oversized_line":
        sys.stdout.write("x" * (1024 * 1024) + "\n")
        sys.stdout.flush()
        wait_for_stop()
    elif mode == "invalid_line":
        sys.stdout.flush()
        sys.stdout.buffer.write(b"\xff\n")
        sys.stdout.buffer.flush()
        wait_for_stop()
    elif mode == "ignore_stop":
        while True:
            time.sleep(0.1)
    else:
        ready()
        wait_for_stop()
    '''
).lstrip()


class RecordingIngress:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.started = asyncio.Event()

    async def handle_stream(self, stream, *, peer: str) -> None:
        self.started.set()
        self.calls.append((peer, await stream.read(1024)))


class RecordingRendezvous(DirectRendezvous):
    def __init__(self, clock=None) -> None:
        super().__init__(clock=clock or time.time)
        self.published: list[tuple[DirectEndpoint | None, str]] = []

    def publish(self, endpoint, *, reason="not_running"):
        self.published.append((endpoint, reason))
        super().publish(endpoint, reason=reason)


@pytest.fixture
async def sidecar(tmp_path: Path):
    path = tmp_path / "stand-in-sidecar.py"
    path.write_text(SIDECAR, encoding="utf-8")
    path.chmod(0o700)
    events = tmp_path / "events.jsonl"
    env = {
        "DIRECT_TEST_EVENTS": str(events),
        "DIRECT_TEST_GENERATIONS": str(tmp_path / "generations"),
        "DIRECT_TEST_HOSTNAME": HOSTNAME,
    }
    yield path, events, env


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def _events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _admission_notices(path: Path) -> list[dict]:
    return [item["message"] for item in _events(path) if item["kind"] == "admission_notice"]


@pytest.mark.asyncio
async def test_ready_addresses_and_restart_publish_each_generation(sidecar):
    """Protect endpoint publication, address refresh, new tokens, and generations."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "ready_addresses"
    rendezvous = RecordingRendezvous()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), RecordingIngress(), rendezvous)
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.diagnostics.endpoint_id == ENDPOINT_ONE)
        first_revision = rendezvous._revision
        await _wait_until(lambda: rendezvous._revision > first_revision)
        assert supervisor.diagnostics.endpoint_id == ENDPOINT_ONE
        assert rendezvous._candidates[0]["address"] == "192.168.1.21:51235"
        assert json.loads(events.read_text(encoding="utf-8").splitlines()[0])["start"]["type"] == "start"
        first_token = _events(events)[0]["start"]["token"]
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)

    # A fresh supervisor generation receives a fresh sidecar key and publishes
    # it without relying on any product caller.
    env["DIRECT_TEST_MODE"] = "restart"
    Path(env["DIRECT_TEST_GENERATIONS"]).unlink(missing_ok=True)
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(
        str(path),
        RecordingIngress(),
        rendezvous,
        restart_initial_delay_seconds=0.02,
        restart_max_delay_seconds=0.05,
    )
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.diagnostics.endpoint_id == ENDPOINT_TWO)
        assert any(endpoint is None and reason == "sidecar_exited" for endpoint, reason in rendezvous.published)
        assert supervisor.diagnostics.endpoint_id == ENDPOINT_TWO
        starts = [item["start"] for item in _events(events) if item["kind"] == "start"]
        assert starts[-1]["token"] != first_token
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_admission_notices_follow_rendezvous_admissions(sidecar):
    """Protect pre-answer admission notices, extensions, and sidecar-local state."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "admission"
    rendezvous = RecordingRendezvous()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), RecordingIngress(), rendezvous)
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.diagnostics.endpoint_id == ENDPOINT_ONE)
        first = await asyncio.to_thread(
            rendezvous.offer,
            instance_id="inst_test",
            hostname=HOSTNAME,
            subject="subject",
            client_endpoint_id=PEER,
        )
        assert first["status"] == "ready"
        await _wait_until(lambda: len(_admission_notices(events)) == 1)
        second = await asyncio.to_thread(
            rendezvous.offer,
            instance_id="inst_test",
            hostname=HOSTNAME,
            subject="subject",
            client_endpoint_id=PEER,
        )
        assert second["expires_at"] > first["issued_at"]
        await _wait_until(lambda: len(_admission_notices(events)) == 2)
        assert _admission_notices(events) == [
            {"type": "admission", "endpoint_id": PEER, "ttl_seconds": 300},
            {"type": "admission", "endpoint_id": PEER, "ttl_seconds": 300},
        ]
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_valid_header_reaches_ingress_with_endpoint_label(sidecar):
    """Protect the sole data door and the peer label passed to ingress."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "stream"
    ingress = RecordingIngress()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), ingress, RecordingRendezvous())
    try:
        await supervisor.start()
        await asyncio.wait_for(ingress.started.wait(), timeout=5)
        assert ingress.calls == [(PEER, b"payload")]
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_real_direct_ingress_answers_connect_from_stream_listener(sidecar, tmp_path):
    """Protect the real CONNECT handoff instead of only a mocked ingress call."""
    path, events, env = sidecar
    certfile, keyfile = _issue_certificate(tmp_path, HOSTNAME)
    ingress = DirectIngress(
        lambda scope, receive, send: None,
        hostname=HOSTNAME,
        certfile=str(certfile),
        keyfile=str(keyfile),
    )
    env["DIRECT_TEST_MODE"] = "real_ingress"
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), ingress, RecordingRendezvous())
    try:
        await ingress.start()
        await supervisor.start()
        await _wait_until(lambda: any(item["kind"] == "connect_response" for item in _events(events)))
        response = next(item["response"] for item in _events(events) if item["kind"] == "connect_response")
        assert response.startswith("HTTP/1.1 200 Connection Established")
    finally:
        await supervisor.stop()
        await ingress.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_wrong_token_malformed_oversized_and_incomplete_headers_are_refused(sidecar):
    """Protect silent closure for every malformed or incomplete sidecar header."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "bad_headers"
    ingress = RecordingIngress()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), ingress, RecordingRendezvous())
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.diagnostics.refusals == 4, timeout=7)
        assert ingress.calls == []
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("wrong_protocol", "protocol_mismatch"),
        ("unknown_message", "unknown_message"),
        ("oversized_line", "oversized_line"),
        ("invalid_line", "invalid_line"),
    ],
)
async def test_invalid_sidecar_messages_withdraw_and_restart(sidecar, mode, reason):
    """Protect fail-closed control parsing and bounded recovery for bad messages."""
    path, _events_path, env = sidecar
    env["DIRECT_TEST_MODE"] = mode
    rendezvous = RecordingRendezvous()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(
        str(path),
        RecordingIngress(),
        rendezvous,
        restart_initial_delay_seconds=0.02,
        restart_max_delay_seconds=0.05,
    )
    try:
        await supervisor.start()
        await _wait_until(lambda: any(endpoint is None and value == reason for endpoint, value in rendezvous.published))
        assert supervisor.diagnostics.reason == reason or supervisor.diagnostics.running
        assert len([item for item in rendezvous.published if item[0] is None and item[1] == reason]) >= 1
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_fatal_withdraws_immediately_and_restarts_with_backoff(sidecar):
    """Protect immediate rendezvous withdrawal and non-tight fatal recovery."""
    path, _events_path, env = sidecar
    env["DIRECT_TEST_MODE"] = "fatal_restart"
    rendezvous = RecordingRendezvous()
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(
        str(path),
        RecordingIngress(),
        rendezvous,
        restart_initial_delay_seconds=0.05,
        restart_max_delay_seconds=0.1,
    )
    try:
        await supervisor.start()
        await _wait_until(lambda: any(endpoint is None and reason == "fatal:synthetic_failure" for endpoint, reason in rendezvous.published))
        fatal_index = next(
            index for index, item in enumerate(rendezvous.published) if item[0] is None and item[1] == "fatal:synthetic_failure"
        )
        await _wait_until(lambda: supervisor.diagnostics.endpoint_id == ENDPOINT_TWO)
        assert any(endpoint is not None for endpoint, _reason in rendezvous.published[fatal_index + 1 :])
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_missing_executable_warning_is_once_per_start(tmp_path, caplog):
    """Keep a missing runtime retryable without repeating the warning every backoff."""
    caplog.set_level(logging.WARNING, logger="vibe.direct_transport")
    rendezvous = RecordingRendezvous()
    supervisor = DirectTransportSupervisor(
        str(tmp_path / "missing-sidecar"),
        RecordingIngress(),
        rendezvous,
        restart_initial_delay_seconds=0.01,
        restart_max_delay_seconds=0.01,
    )
    try:
        await supervisor.start()
        await _wait_until(
            lambda: len([reason for _endpoint, reason in rendezvous.published if reason == "executable_not_found"]) >= 3
        )
        messages = [record for record in caplog.records if record.name == "vibe.direct_transport"]
        assert len(messages) == 1
        await supervisor.stop()

        await supervisor.start()
        await _wait_until(
            lambda: len([reason for _endpoint, reason in rendezvous.published if reason == "executable_not_found"]) >= 6
        )
        messages = [record for record in caplog.records if record.name == "vibe.direct_transport"]
        assert len(messages) == 2
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_cancelled_start_stops_supervisor(sidecar, monkeypatch):
    """Protect cancellation after the supervisor task exists."""
    path, _events_path, env = sidecar
    env["DIRECT_TEST_MODE"] = "wait"
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), RecordingIngress(), RecordingRendezvous())
    original_sleep = asyncio.sleep
    gate = asyncio.Event()

    async def hold_initial_yield(delay):
        if delay == 0:
            await gate.wait()
        else:
            await original_sleep(delay)

    monkeypatch.setattr(direct_transport.asyncio, "sleep", hold_initial_yield)
    start_task = asyncio.create_task(supervisor.start())
    try:
        for _ in range(100):
            if supervisor._supervisor_task is not None:
                break
            await original_sleep(0)
        assert supervisor._supervisor_task is not None
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        assert supervisor._supervisor_task is None
        assert not supervisor._started
        assert not supervisor.running
    finally:
        gate.set()
        if not start_task.done():
            start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start_task
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_stop_closes_listener_and_reaps_well_behaved_child(sidecar):
    """Protect graceful stop, listener closure, child reaping, and task cleanup."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "wait"
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), RecordingIngress(), RecordingRendezvous())
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.running)
        await _wait_until(lambda: any(item["kind"] == "start" for item in _events(events)))
        pid = next(item["pid"] for item in _events(events) if item["kind"] == "start")
        port = supervisor._stream_port
        await supervisor.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert port is not None
        with socket.socket() as sock:
            with pytest.raises(OSError):
                sock.connect(("127.0.0.1", port))
        assert supervisor._supervisor_task is None
        assert not supervisor._stream_tasks
    finally:
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_sidecar_exits_when_owner_control_pipe_closes(sidecar):
    """The child does not outlive the owner when its control pipe closes."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "wait"
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(str(path), RecordingIngress(), RecordingRendezvous())
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.running)
        await _wait_until(lambda: any(item["kind"] == "start" for item in _events(events)))
        pid = next(item["pid"] for item in _events(events) if item["kind"] == "start")
        process = supervisor._process
        assert process is not None and process.stdin is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        await _wait_until(lambda: _process_gone(pid))
    finally:
        await supervisor.stop()
        os.environ.clear()
        os.environ.update(old_env)


@pytest.mark.asyncio
async def test_child_exits_when_owner_is_killed_without_stop(sidecar):
    """Real parent death closes the control pipe, without any Python cleanup."""
    path, events, env = sidecar
    code = '''
import asyncio, sys
from vibe.direct_transport import DirectTransportSupervisor
from vibe.direct_rendezvous import DirectRendezvous
async def main():
    supervisor = DirectTransportSupervisor(sys.argv[1], None, DirectRendezvous())
    await supervisor.start()
    await asyncio.Event().wait()
asyncio.run(main())
'''
    parent = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, str(path),
        env={**os.environ, **env}, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    child = None
    try:
        await _wait_until(lambda: any(item["kind"] == "start" for item in _events(events)))
        child = psutil.Process(next(item["pid"] for item in _events(events) if item["kind"] == "start"))
        parent.kill()
        await asyncio.wait_for(parent.wait(), 3)
        await _wait_until(lambda: not child.is_running() or child.status() == psutil.STATUS_ZOMBIE)
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()
        if child is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    child.kill()  # Only this test's captured PID/creation identity.


def _process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@pytest.mark.asyncio
async def test_stop_kills_child_that_ignores_control_close(sidecar):
    """Protect PID-only escalation when a child ignores its closed control pipe."""
    path, events, env = sidecar
    env["DIRECT_TEST_MODE"] = "ignore_stop"
    old_env = os.environ.copy()
    os.environ.update(env)
    supervisor = DirectTransportSupervisor(
        str(path),
        RecordingIngress(),
        RecordingRendezvous(),
        stop_grace_seconds=0.05,
    )
    try:
        await supervisor.start()
        await _wait_until(lambda: supervisor.running)
        await _wait_until(lambda: any(item["kind"] == "start" for item in _events(events)))
        pid = next(item["pid"] for item in _events(events) if item["kind"] == "start")
        await supervisor.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def _issue_certificate(directory: Path, hostname: str) -> tuple[Path, Path]:
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "cert.pem"
    keyfile = directory / "key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyfile.chmod(0o600)
    return certfile, keyfile
