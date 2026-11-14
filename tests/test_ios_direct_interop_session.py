"""Real TCP/control dispatch with a Python protocol fixture, NOT native QUIC.

The peer opens ingress itself; only the hosted published-sidecar/App job can
prove native causal forwarding and closure. This test proves the host's six-case
state machine, separate tier handling, artifact join and recorded-PID cleanup.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from tests.test_ios_direct_interop_gate import runner
from tests.test_ios_direct_interop_host import connect_ingress, host, probe


@pytest.mark.asyncio
async def test_six_case_session_and_transport_skip_reconcile(tmp_path):
    child = tmp_path / "protocol-fixture"
    child.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys
start = json.loads(sys.stdin.readline())
pathlib.Path("fixture-start.json").write_text(json.dumps(start))
print(json.dumps({"type":"ready", "protocol":1, "endpoint_id":"d"*64, "addresses":["127.0.0.1:1234"]}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with open("fixture-commands.jsonl", "a") as out:
        out.write(json.dumps(command) + "\\n")
    if command["type"] == "admission" and command["ttl_seconds"] == 0:
        print(json.dumps({"type":"peer_closed", "endpoint_id":command["endpoint_id"], "reason":"admission_ended"}), flush=True)
    if command["type"] == "stop":
        break
''')
    child.chmod(0o700)
    directory = tmp_path / "results"
    supervisor = host.Host(child, directory)
    fixture = json.loads((await supervisor.start()).read_text())
    port = int(fixture["control"].split(":")[1])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    app_cases = []
    names = ["admitted", "not_admitted", "wrong_identity", "refused_connect", "admission_ends", "sidecar_stops"]
    try:
        await host.send_message(writer, {"type": "hello", "schema": 2, "tier": "engine"})
        for index, name in enumerate(names, 1):
            client_id = f"{index:064x}"
            await host.send_message(writer, {"type": "next", "client_endpoint_id": client_id})
            offer = await host.read_message(reader, 10)
            assert offer["name"] == name and offer["generation"] == "interop-v2-1"
            assert offer["sidecar_endpoint_id"] == ("58" + "66" * 31 if name == "wrong_identity" else "d" * 64)
            start = json.loads((directory / "engine" / name / "fixture-start.json").read_text())
            ingress_port = int(start["stream_addr"].split(":")[1])
            ingress = SimpleNamespace(token=start["token"], client_id=client_id)
            observed = {"control_tcp": True}
            if name in {"not_admitted", "wrong_identity"}:
                observed.update(failure="handshake", case_streams=0)
            else:
                assert await probe(ingress, ingress_port) == b"HTTP/1.1 200 Connection Established\r\n\r\n"
                barrier = {"type": "setup_done", "schema": 2, "name": name}
                await host.send_message(writer, barrier)
                assert await host.read_message(reader, 3) == barrier
                observed.update(setup_attempts=1, setup_probe={"status": 200, "streams": 1})
                status = 403 if name == "refused_connect" else 200
                assert (await probe(ingress, ingress_port)).startswith(f"HTTP/1.1 {status} ".encode())
                observed["case_probe"] = {"status": status}
                if name == "refused_connect":
                    observed["failure"] = "probe_refused"
                else:
                    payload_reader, payload_writer = await connect_ingress(ingress, ingress_port)
                    payload_writer.write(b"avibe-direct-interop-v2\n")
                    await payload_writer.drain()
                    assert await asyncio.wait_for(payload_reader.readexactly(24), 2) == b"avibe-direct-interop-v2\n"
                    observed.update(dial="established", remote_identity="matched",
                                    payload={"sent_bytes": 24, "received_bytes": 24})
                    if name == "admitted":
                        payload_writer.write_eof()
                        assert await asyncio.wait_for(payload_reader.read(), 2) == b""
                        observed["payload"].update(sent_fin=True, received_fin=True)
                    else:
                        await host.send_message(writer, {"type": "armed", "schema": 2, "name": name})
                        assert await host.read_message(reader, 3) == {"type": "triggered", "schema": 2, "name": name}
                        observed.update(armed_alive=True, closed_after_armed=True)
                    # Python fixture closure, not evidence of native causality.
                    await host.close_writer(payload_writer)
            result = {"name": name, "outcome": "passed", "observed": observed, "detail": None}
            await host.send_message(writer, {"type": "result", "schema": 2, **result})
            verdict = await host.read_message(reader, 10)
            assert verdict == {"type": "verdict", "schema": 2, "name": name, "host": "confirmed", "detail": None}
            app_cases.append(result)
        assert await host.read_message(reader, 2) == {"type": "done", "schema": 2}
        host.write_json(directory / "engine.json", {"schema": 2, "tier": "engine", "status": "passed", "cases": app_cases})
        await host.close_writer(writer)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await host.send_message(writer, {"type": "hello", "schema": 2, "tier": "transport"})
        await host.send_message(writer, {"type": "next", "client_endpoint_id": "e" * 64})
        offer = await host.read_message(reader, 10)
        assert offer["admissible_addresses"] == []
        await host.send_message(writer, {"type": "result", "schema": 2, "name": "admitted", "outcome": "not_covered", "observed": {}, "detail": None})
        assert (await host.read_message(reader, 10))["host"] == "confirmed"
        assert (await host.read_message(reader, 2))["type"] == "done"
        host.write_json(directory / "transport.json", {"schema": 2, "tier": "transport", "status": "not_covered", "cases": []})
        await asyncio.wait_for(supervisor.complete.wait(), 3)
    finally:
        await host.close_writer(writer)
        await supervisor.close()
    assert runner.reconcile(directory, 0) == {"status": "passed", "engine": "passed", "transport": "not_covered"}
    for name in names:
        commands = [json.loads(line) for line in (directory / "engine" / name / "fixture-commands.jsonl").read_text().splitlines()]
        admissions = [c for c in commands if c["type"] == "admission"]
        assert bool(admissions) == (name != "not_admitted")
        pid = json.loads((directory / "engine" / name / "pid.json").read_text())["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
