"""Real TCP/control dispatch with a Python protocol fixture, NOT native QUIC.

The peer opens ingress itself; only the hosted published-sidecar/App job can
prove native causal forwarding and closure. This test proves the host's six-case
state machine, separate tier handling, artifact join and recorded-PID cleanup.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from tests.test_ios_direct_interop_gate import runner
from tests.test_ios_direct_interop_host import connect_ingress, host, probe


@pytest.mark.asyncio
@pytest.mark.parametrize("late_case", [None, "admitted", "not_admitted"])
async def test_six_case_session_and_transport_skip_reconcile(tmp_path, late_case):
    child = tmp_path / "protocol-fixture"
    child.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, socket, sys
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
        if pathlib.Path.cwd().name == LATE_CASE and pathlib.Path.cwd().parent.name == "engine":
            client = 1 if LATE_CASE == "admitted" else 2
            address, port = start["stream_addr"].split(":")
            with socket.create_connection((address, int(port)), timeout=2) as stream:
                stream.sendall((f'AVDT1 {start["token"]} {client:064x}\\n' +
                                'CONNECT interop.invalid:443 HTTP/1.1\\r\\nHost: interop.invalid:443\\r\\n\\r\\n').encode())
                assert stream.recv(4096).startswith(b"HTTP/1.1 200 ")
        break
'''.replace("LATE_CASE", repr(late_case)))
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
            if name == late_case:
                assert verdict["host"] == "contradicted", "shutdown ingress was omitted from final verdict"
            else:
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
    if late_case is None:
        assert runner.reconcile(directory, 0) == {"status": "passed", "engine": "passed", "transport": "not_covered"}
    else:
        with pytest.raises(runner.host.HostFault, match="engine did not pass"):
            runner.reconcile(directory, 0)
        record = next(c for c in json.loads((directory / "host.json").read_text())["cases"] if c["name"] == late_case)
        assert record["observed"]["case_streams"] == (3 if late_case == "admitted" else 1)
    for name in names:
        commands = [json.loads(line) for line in (directory / "engine" / name / "fixture-commands.jsonl").read_text().splitlines()]
        admissions = [c for c in commands if c["type"] == "admission"]
        assert bool(admissions) == (name != "not_admitted")
        pid = json.loads((directory / "engine" / name / "pid.json").read_text())["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
async def test_host_cancellation_cannot_abandon_ignoring_stop_child(tmp_path, monkeypatch):
    child = tmp_path / "ignoring-stop-fixture"
    child.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, signal, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
json.loads(sys.stdin.readline())
print(json.dumps({"type":"ready", "protocol":1, "endpoint_id":"d"*64, "addresses":["127.0.0.1:1234"]}), flush=True)
for line in sys.stdin:
    if json.loads(line)["type"] == "stop":
        pathlib.Path("stop-seen").touch()
''')
    child.chmod(0o700)
    cases = []
    original_case = host.Case

    def track_case(*args, **kwargs):
        case = original_case(*args, **kwargs)
        cases.append(case)
        return case

    monkeypatch.setattr(host, "Case", track_case)
    supervisor = host.Host(child, tmp_path / "results")
    fixture = json.loads((await supervisor.start()).read_text())
    reader, writer = await asyncio.open_connection("127.0.0.1", int(fixture["control"].split(":")[1]))
    closing = None
    try:
        await host.send_message(writer, {"type": "hello", "schema": 2, "tier": "engine"})
        await host.send_message(writer, {"type": "next", "client_endpoint_id": "a" * 64})
        assert (await host.read_message(reader, 5))["name"] == "admitted"
        await host.send_message(writer, {"type": "result", "schema": 2, "name": "admitted",
                                         "outcome": "failed", "observed": {}, "detail": "fixture failure"})
        marker = tmp_path / "results/engine/admitted/stop-seen"
        async with asyncio.timeout(2):
            while not marker.exists():
                await asyncio.sleep(0.01)
        # The serving task is already in Case.close -> Sidecar.close. Closing
        # the host cancels that task; also cancel Host.close itself twice.
        closing = asyncio.create_task(supervisor.close())
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0.01)
        closing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(closing), 12)
        process = cases[0].sidecar.process
        assert process.returncode is not None, "recorded sidecar PID survived cancelled cleanup"
        assert process.returncode == -signal.SIGKILL
        assert all(task.done() for task in cases[0].sidecar.tasks)
        assert not cases[0].ingress.tasks
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        # Also reap on a regression failure; a failing source test must never
        # leak its own deliberately uncooperative child onto this desktop.
        if cases and cases[0].sidecar.process:
            process = cases[0].sidecar.process
            if process.returncode is None:
                process.kill()
            await process.wait()
            await asyncio.gather(*cases[0].sidecar.tasks, return_exceptions=True)
            await cases[0].ingress.close()
        await host.close_writer(writer)
        if closing is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await closing
        await supervisor.close()


@pytest.mark.asyncio
async def test_transport_address_loss_cannot_erase_completed_coverage(tmp_path):
    child = tmp_path / "changing-address-fixture"
    child.write_text(f"#!{sys.executable}\n" + '''
import json, pathlib, sys
start = json.loads(sys.stdin.readline())
pathlib.Path("fixture-start.json").write_text(json.dumps(start))
addresses = ["10.0.0.1:1234"] if pathlib.Path.cwd().name == "admitted" else ["127.0.0.1:1234"]
print(json.dumps({"type":"ready", "protocol":1, "endpoint_id":"d"*64, "addresses":addresses}), flush=True)
for line in sys.stdin:
    if json.loads(line)["type"] == "stop":
        break
''')
    child.chmod(0o700)
    directory = tmp_path / "results"
    supervisor = host.Host(child, directory)
    fixture = json.loads((await supervisor.start()).read_text())
    reader, writer = await asyncio.open_connection("127.0.0.1", int(fixture["control"].split(":")[1]))
    try:
        await host.send_message(writer, {"type": "hello", "schema": 2, "tier": "transport"})
        await host.send_message(writer, {"type": "next", "client_endpoint_id": "1" * 64})
        assert (await host.read_message(reader, 5))["admissible_addresses"] == ["10.0.0.1:1234"]
        start = json.loads((directory / "transport/admitted/fixture-start.json").read_text())
        ingress = SimpleNamespace(token=start["token"], client_id="1" * 64)
        port = int(start["stream_addr"].split(":")[1])
        await probe(ingress, port)
        barrier = {"type": "setup_done", "schema": 2, "name": "admitted"}
        await host.send_message(writer, barrier)
        assert await host.read_message(reader, 3) == barrier
        await probe(ingress, port)
        payload_reader, payload_writer = await connect_ingress(ingress, port)
        payload_writer.write(b"avibe-direct-interop-v2\n")
        await payload_writer.drain()
        assert await payload_reader.readexactly(24) == b"avibe-direct-interop-v2\n"
        payload_writer.write_eof()
        assert await payload_reader.read() == b""
        await host.close_writer(payload_writer)
        await host.send_message(writer, {"type": "result", "schema": 2, "name": "admitted", "outcome": "passed", "detail": None,
            "observed": {"control_tcp": True, "setup_attempts": 1, "setup_probe": {"status": 200, "streams": 1},
                         "case_probe": {"status": 200}, "dial": "established", "remote_identity": "matched",
                         "payload": {"sent_bytes": 24, "received_bytes": 24, "sent_fin": True, "received_fin": True}}})
        assert (await host.read_message(reader, 5))["host"] == "confirmed"
        await host.send_message(writer, {"type": "next", "client_endpoint_id": "2" * 64})
        offer = await host.read_message(reader, 5)
        assert offer["name"] == "not_admitted" and offer["admissible_addresses"] == []
        await host.send_message(writer, {"type": "result", "schema": 2, "name": "not_admitted",
                                         "outcome": "not_covered", "observed": {}, "detail": None})
        verdict = await host.read_message(reader, 5)
        assert verdict["host"] == "contradicted", "a later empty offer erased already completed coverage"
        await host.close_writer(writer)
        await asyncio.wait_for(asyncio.gather(*tuple(supervisor.tasks)), 3)
        evidence = json.loads((directory / "host.json").read_text())["transport"]
        assert evidence["status"] == "failed"
        assert [case["name"] for case in evidence["cases"]] == ["admitted", "not_admitted"]
        assert evidence["cases"][0]["host"] == "confirmed"
    finally:
        await host.close_writer(writer)
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("leader_state", ["exits_on_term", "already_exited"])
async def test_runner_reaps_residual_group_after_leader_exit(tmp_path, leader_state):
    ready = tmp_path / "descendant-ready"
    published = tmp_path / "descendant-pid"
    child_code = (
        "import os,pathlib,signal,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    leader_code = f'''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(ready)!r}])
while not pathlib.Path({str(ready)!r}).exists():
    time.sleep(0.01)
pathlib.Path({str(published)!r}).write_text(str(child.pid))
if {leader_state!r} == "already_exited":
    sys.exit(0)
time.sleep(60)
'''
    fixture_host = host.Host(tmp_path / "unused", tmp_path / "results")
    await fixture_host.start()
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", leader_code,
                                                   start_new_session=True,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(3):
            while not published.exists():
                await asyncio.sleep(0.01)
        descendant = int(published.read_text())
        if leader_state == "already_exited":
            await asyncio.wait_for(process.wait(), 3)
            assert process.returncode == 0
        await asyncio.wait_for(runner.close_run(process, fixture_host), 12)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.kill(descendant, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        # A failing regression still terminates only its recorded fresh-session
        # group; never pattern-kill or touch any existing desktop process.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(3):
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        pass
                    await asyncio.sleep(0.01)
        await fixture_host.close()
