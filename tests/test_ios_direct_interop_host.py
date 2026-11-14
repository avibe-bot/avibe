"""Source-only tests of host framing, real loopback fixture I/O and owned PIDs.

These protect phase separation, independent byte/FIN evidence, and cleanup.
They do not stand in for the published-sidecar/Swift hosted interoperability run.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "ios/scripts/direct_interop_host.py"
spec = importlib.util.spec_from_file_location("direct_interop_host", SCRIPT)
assert spec and spec.loader
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [
    b'{"type":"next","type":"result"}\n', b'[]\n', b'{"type":null}\n',
    b'{"type":"result","n":NaN}\n', b'\xff\n', b'{"type":"next"}',
    b'{"type":"next","padding":"' + b'x' * 4096 + b'"}\n',
])
async def test_control_rejects_malformed_or_oversized_records(wire):
    reader = asyncio.StreamReader(limit=4096)
    reader.feed_data(wire)
    reader.feed_eof()
    with pytest.raises(host.HostFault):
        await host.read_message(reader, 0.2)


@pytest.mark.asyncio
async def test_control_accepts_exact_4096_bytes_and_preserves_next_message():
    record = {"type": "next", "padding": ""}
    line = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    record["padding"] = "x" * (4096 - len(line))
    reader = asyncio.StreamReader(limit=4096)
    reader.feed_data(json.dumps(record, separators=(",", ":")).encode() + b"\n" + b'{"type":"done"}\n')
    assert (await host.read_message(reader, 0.2))["type"] == "next"
    assert await host.read_message(reader, 0.2) == {"type": "done"}


async def connect_ingress(ingress, port, *, header=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(header if header is not None else f"AVDT1 {ingress.token} {ingress.client_id}\n".encode())
    await writer.drain()
    return reader, writer


async def probe(ingress, port):
    reader, writer = await connect_ingress(ingress, port)
    writer.write(b"CONNECT interop.invalid:443 HTTP/1.1\r\nHost: interop.invalid:443\r\n\r\n")
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 1)
    writer.write_eof()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    await host.close_writer(writer)
    return response


@pytest.mark.asyncio
async def test_setup_200_is_separate_from_refused_case_and_count():
    ingress = host.Ingress("a" * 64, "b" * 64, host.Journal())
    port = await ingress.start()
    try:
        assert await probe(ingress, port) == b"HTTP/1.1 200 Connection Established\r\n\r\n"
        await ingress.finish_setup(refused=True)
        assert await probe(ingress, port) == b"HTTP/1.1 403 Forbidden\r\n\r\n"
        assert [(s["phase"], s["status"]) for s in ingress.streams] == [("setup", 200), ("case", 403)]
        assert not ingress.errors
    finally:
        await ingress.close()
    assert not ingress.tasks


@pytest.mark.asyncio
async def test_payload_counts_hash_and_fin_are_independent_socket_observations():
    ingress = host.Ingress("a" * 64, "b" * 64, host.Journal())
    port = await ingress.start()
    try:
        await probe(ingress, port)
        await ingress.finish_setup(refused=False)
        reader, writer = await connect_ingress(ingress, port)
        payload = b"avibe-direct-interop-v2\n"
        writer.write(payload)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(24), 1) == payload
        assert not ingress.streams[-1]["eof"]
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        await host.close_writer(writer)
        stream = ingress.streams[-1]
        assert stream["sent"] == stream["received"] == 24
        assert stream["sha256"] == "91cefa0d9c7ccf4ddd520ab3514f51c036557939bcb140c40448cc9ebf0cd4df"
        assert stream["eof"] and not stream["reset"]
        assert not ingress.errors
    finally:
        await ingress.close()


@pytest.mark.asyncio
async def test_bad_header_counts_as_ingress_and_cannot_pass_a_negative_case():
    ingress = host.Ingress("a" * 64, "b" * 64, host.Journal())
    port = await ingress.start()
    try:
        reader, writer = await connect_ingress(ingress, port, header=b"AVDT1 wrong client\n")
        assert await asyncio.wait_for(reader.read(), 1) == b""
        await host.close_writer(writer)
        assert len(ingress.streams) == 1
        assert ingress.errors == ["bad ingress AVDT1 header"]
        assert not ingress.streams[0]["authenticated"]
    finally:
        await ingress.close()


@pytest.mark.asyncio
async def test_listener_cleanup_cancels_stalled_header_and_closes_owned_socket():
    ingress = host.Ingress("a" * 64, "b" * 64, host.Journal())
    port = await ingress.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await ingress.close()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    await host.close_writer(writer)
    assert not ingress.tasks
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_sidecar_supervisor_records_reaps_only_its_child_and_drains_pipes(tmp_path):
    # Deliberately a protocol fixture, not native interop evidence.
    child = tmp_path / "protocol-fixture"
    child.write_text(f"#!{sys.executable}\n" + '''
import json, sys
json.loads(sys.stdin.readline())
print(json.dumps({"type":"ready", "protocol":1, "endpoint_id":"a"*64, "addresses":["127.0.0.1:1234"]}), flush=True)
sys.stderr.write("fixture diagnostic\\n")
sys.stderr.flush()
for line in sys.stdin:
    if json.loads(line)["type"] == "stop":
        break
''')
    child.chmod(0o700)
    journal = host.Journal()
    sidecar = host.Sidecar(child, tmp_path, journal)
    try:
        ready = await sidecar.start(1234, "b" * 64)
        assert ready["endpoint_id"] == "a" * 64
        pid = sidecar.process.pid
        assert json.loads((tmp_path / "pid.json").read_text())["pid"] == pid
    finally:
        await sidecar.close()
    assert sidecar.process.returncode == 0
    assert all(task.done() for task in sidecar.tasks)
    assert not sidecar.errors
    assert (tmp_path / "sidecar.stderr").read_text() == "fixture diagnostic\n"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
