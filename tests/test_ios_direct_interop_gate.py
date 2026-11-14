"""Independent verdict, cross-socket ordering and pinned-input gate contracts.

Fabricated artifact inputs here are rejection tests, never native evidence.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import sys
import tarfile

import pytest

from tests.test_ios_direct_interop_host import SCRIPT, connect_ingress, host, probe

# The command-line modules intentionally live outside product imports.
sys.path.insert(0, str(SCRIPT.parent))
try:
    import run_direct_interop as runner
finally:
    sys.path.pop(0)


@pytest.mark.asyncio
async def test_setup_ack_is_a_real_cross_socket_barrier(tmp_path):
    case = host.Case("refused_connect", "a" * 64, tmp_path / "unused", tmp_path)
    port = await case.ingress.start()
    response_reader, response_writer = await connect_ingress(case.ingress, port)
    response_writer.write(b"CONNECT interop.invalid:443 HTTP/1.1\r\nHost: interop.invalid:443\r\n\r\n")
    await response_writer.drain()
    assert await response_reader.readuntil(b"\r\n\r\n") == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    control_tasks = []

    async def control(reader, writer):
        try:
            assert await host.read_message(reader, 1) == {"type": "setup_done", "schema": 2, "name": "refused_connect"}
            await case.setup_done(writer)
        finally:
            await host.close_writer(writer)

    def accepted(reader, writer):
        control_tasks.append(asyncio.create_task(control(reader, writer)))

    server = await asyncio.start_server(accepted, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        await host.send_message(writer, {"type": "setup_done", "schema": 2, "name": "refused_connect"})
        # Retain setup socket: control write alone cannot ACK/switch phases.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reader.readuntil(b"\n"), 0.05)
        assert case.ingress.phase == "setup" and case.ingress.answer == 200
        response_writer.write_eof()
        assert await response_reader.read() == b""
        await host.close_writer(response_writer)
        assert await host.read_message(reader, 1) == {"type": "setup_done", "schema": 2, "name": "refused_connect"}
        assert await probe(case.ingress, port) == b"HTTP/1.1 403 Forbidden\r\n\r\n"
        assert [s["phase"] for s in case.ingress.streams] == ["setup", "case"]
    finally:
        await host.close_writer(writer)
        await host.close_writer(response_writer)
        await case.ingress.close()
        await asyncio.gather(*control_tasks)
        server.close()
        await server.wait_closed()


def admitted_evidence(tmp_path):
    case = host.Case("admitted", "a" * 64, tmp_path / "unused", tmp_path)
    case.ingress.phase = "case"
    case.ingress.streams = [
        {"index": 0, "phase": "setup", "kind": "probe", "status": 200, "authenticated": True},
        {"index": 1, "phase": "case", "kind": "probe", "status": 200, "authenticated": True},
        {"index": 2, "phase": "case", "kind": "payload", "authenticated": True,
         "received": 24, "sent": 24, "sha256": "91cefa0d9c7ccf4ddd520ab3514f51c036557939bcb140c40448cc9ebf0cd4df",
         "eof": True, "reset": False, "closed": True},
    ]
    app = {"name": "admitted", "outcome": "passed", "detail": None, "observed": {
        "control_tcp": True, "setup_attempts": 1, "dial": "established", "remote_identity": "matched",
        "setup_probe": {"status": 200, "streams": 1}, "case_probe": {"status": 200},
        "payload": {"sent_bytes": 24, "received_bytes": 24, "sent_fin": True, "received_fin": True},
    }}
    return case, app


@pytest.mark.parametrize("mutation", ["bytes", "hash", "reset", "identity", "extra_stream", "setup_count", "app_failed", "child_failed"])
def test_app_pass_cannot_override_independent_host_contradictions(tmp_path, mutation):
    case, app = admitted_evidence(tmp_path)
    assert case.confirm(app)["fin_seen"] is True
    if mutation == "bytes":
        case.ingress.streams[-1]["sent"] = 23
    elif mutation == "hash":
        case.ingress.streams[-1]["sha256"] = "0" * 64
    elif mutation == "reset":
        case.ingress.streams[-1]["reset"] = True
    elif mutation == "identity":
        case.ingress.streams[-1]["authenticated"] = False
    elif mutation == "extra_stream":
        case.ingress.streams.append(dict(case.ingress.streams[1]))
    elif mutation == "setup_count":
        app["observed"]["setup_probe"]["streams"] = 2
    elif mutation == "app_failed":
        app["outcome"] = "failed"
    else:
        case.sidecar.errors.append("unexpected sidecar exit")
    with pytest.raises(host.HostFault):
        case.confirm(app)


@pytest.mark.parametrize("name", ["not_admitted", "wrong_identity"])
def test_negative_verdict_does_not_invent_reasons_or_accept_deadline(tmp_path, name):
    case = host.Case(name, "a" * 64, tmp_path / "unused", tmp_path)
    app = {"outcome": "passed", "observed": {"control_tcp": True, "failure": "handshake", "case_streams": 0}}
    observed = case.confirm(app)
    assert observed["case_streams"] == 0 and "peer_closed_reason" not in observed
    app["observed"]["failure"] = "local_deadline"
    with pytest.raises(host.HostFault, match="rejection"):
        case.confirm(app)


def result_artifacts(directory):
    document = {"schema": 2, "tier": "engine", "status": "passed", "cases": []}
    evidence = {"schema": 2, "tier": "engine", "status": "passed", "cases": [], "errors": []}
    names = ["admitted", "not_admitted", "wrong_identity", "refused_connect", "admission_ends", "sidecar_stops"]
    for name in names:
        case = {"name": name, "outcome": "passed", "observed": {}, "detail": None}
        document["cases"].append(case)
        evidence["cases"].append({"name": name, "host": "confirmed", "app_result_sha256": hashlib.sha256(
            json.dumps(case, sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    transport = {"schema": 2, "tier": "transport", "status": "not_covered", "cases": []}
    evidence["transport"] = copy.deepcopy(transport)
    host.write_json(directory / "engine.json", document)
    host.write_json(directory / "transport.json", transport)
    host.write_json(directory / "host.json", evidence)
    return document, evidence


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "changed_result", "contradicted", "skip_engine", "host_error", "xcode_failure"])
def test_final_reconciliation_fails_closed(tmp_path, mutation):
    document, evidence = result_artifacts(tmp_path)
    assert runner.reconcile(tmp_path, 0)["engine"] == "passed"
    code = 0
    if mutation == "missing":
        document["cases"].pop()
    elif mutation == "duplicate":
        document["cases"][-1] = document["cases"][0]
    elif mutation == "changed_result":
        document["cases"][0]["observed"]["different_after_wire_result"] = True
    elif mutation == "contradicted":
        evidence["cases"][0]["host"] = "contradicted"
    elif mutation == "skip_engine":
        document["status"] = evidence["status"] = "not_covered"
    elif mutation == "host_error":
        evidence["errors"] = ["unexpected extra control connection"]
    else:
        code = 65
    host.write_json(tmp_path / "engine.json", document)
    host.write_json(tmp_path / "host.json", evidence)
    with pytest.raises(runner.host.HostFault):
        runner.reconcile(tmp_path, code)


def sidecar_archive(*, unsafe=False):
    binary = b"deliberately-not-a-native-binary"
    manifest = {"binary_sha256": hashlib.sha256(binary).hexdigest(), "binary_bytes": len(binary),
                "target": "aarch64-apple-darwin", "source_sha": "a" * 40,
                "workflow_run_id": "123", "workflow_run_attempt": 1}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in {"avibe-direct-transport": binary, "manifest.json": json.dumps(manifest).encode()}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if unsafe:
            info = tarfile.TarInfo("escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/outside"
            archive.addfile(info)
    data = output.getvalue()
    tag = "direct-transport-v0.1.0-1"
    name = "avibe-direct-transport-aarch64-apple-darwin.tar.gz"
    pin = {"release_state": "published", "source": "avibe-bot/avibe", "release_tag": tag,
           "provenance": {"source_sha": "a" * 40, "qualification_run_id": "123", "qualification_attempt": 1},
           "archives": {"darwin-arm64": {"name": name, "format": "tar.gz", "bin_path": "avibe-direct-transport",
                                        "url": f"https://github.com/avibe-bot/avibe/releases/download/{tag}/{name}",
                                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                        "binary_sha256": hashlib.sha256(binary).hexdigest()}}}
    return data, pin


@pytest.mark.parametrize("mutation", ["pending", "archive_hash", "binary_hash", "source", "qualification", "url", "symlink"])
def test_staging_rejects_unpublished_or_mismatched_sidecar_inputs(tmp_path, mutation):
    data, pin = sidecar_archive(unsafe=mutation == "symlink")
    archive = pin["archives"]["darwin-arm64"]
    if mutation == "pending":
        pin["release_state"] = "pending"
    elif mutation == "archive_hash":
        archive["sha256"] = "0" * 64
    elif mutation == "binary_hash":
        archive["binary_sha256"] = "0" * 64
    elif mutation == "source":
        pin["provenance"]["source_sha"] = "b" * 40
    elif mutation == "qualification":
        pin["provenance"]["qualification_attempt"] = 2
    elif mutation == "url":
        archive["url"] = "https://example.invalid/candidate.tar.gz"
    with pytest.raises(runner.host.HostFault):
        runner.unpack_sidecar(data, pin, "arm64", tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_native_entry_point_refuses_the_live_desktop(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(runner.host.HostFault, match="hosted macOS"):
        runner.hosted_root()
