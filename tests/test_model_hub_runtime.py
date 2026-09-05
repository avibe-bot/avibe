from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import subprocess
import stat
import sys
import tarfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jsonschema import Draft7Validator

from core import managed_runtime
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    EngineHealth,
    OriginNotAllowedError,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    RuntimePlatformUnsupportedError,
    SourceBinding,
)
from core.handlers.model_hub.classification import (
    classify_outcome,
    terminal_outcome_category,
)
from core.handlers.model_hub.request import ModelHubRequest
from core.handlers.model_hub.stream_wire import ProtocolUsageReport
from vibe.model_hub_runtime import adapter as runtime_adapter_module
from vibe.model_hub_runtime import client as client_module
from vibe.model_hub_runtime import installer as runtime_installer_module
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.api_key_vendors import api_key_vendor_catalog
from vibe.model_hub_runtime.client import EngineClient, EngineClientError, EngineConnection
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.environment import engine_subprocess_environment
from vibe.model_hub_runtime.installer import (
    EngineRuntimeManager,
    InstallClaimTransition,
    ManifestResolution,
)
from vibe.model_hub_runtime.state import (
    EngineStateError,
    EngineStateStore,
    SourceRecord,
)
from vibe.model_hub_runtime.supervisor import (
    MODEL_HUB_STARTUP_TIMEOUT_SECONDS,
    EngineSupervisor,
    EngineUnavailableError,
)


MODEL_HUB_FIXTURES = Path(__file__).parent / "fixtures" / "model_hub"
STREAM_TRANSPORT_BOUNDARIES = json.loads(
    (MODEL_HUB_FIXTURES / "stream_transport_boundaries.json").read_text(encoding="utf-8")
)["cases"]
DEEP_JSON_ARRAY = b"[" * 10_000 + b"0" + b"]" * 10_000
RUNTIME_INSTALL_TARGET = {
    "runtime_version": "v7.2.95",
    "platform": "fixture-platform",
    "archive_sha256": "2" * 64,
    "binary_sha256": "3" * 64,
}
RELEASED_INSTALL_CLAIMS = json.loads(
    (MODEL_HUB_FIXTURES / "released_install_claims.json").read_text(encoding="utf-8")
)["claims"]
RUNTIME_INSTALL_GENERATION_A = "a" * 32
RUNTIME_INSTALL_GENERATION_B = "b" * 32
API_KEY_VENDOR_RUNTIME_CASES = tuple(
    [
        *[
            pytest.param(entry.id, entry.protocol, entry.official_base_url, id=entry.id)
            for entry in api_key_vendor_catalog()
        ],
        pytest.param("codex", "openai_responses", "https://api.openai.com/v1", id="codex"),
    ]
)


def _create_runtime_install_claim(
    installer: EngineRuntimeManager,
    *,
    generation: str = RUNTIME_INSTALL_GENERATION_A,
) -> str:
    assert installer.transition_install_claim(
        InstallClaimTransition.CREATE,
        generation=generation,
        target=RUNTIME_INSTALL_TARGET,
    )
    return generation


@pytest.mark.parametrize(
    "released_claim",
    RELEASED_INSTALL_CLAIMS,
    ids=lambda claim: f"schema-{claim['schema_version']}",
)
def test_released_install_claim_is_read_without_rewrite_and_resumed_as_current_schema(
    tmp_path: Path,
    released_claim: dict[str, object],
) -> None:
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    managed_runtime.write_json_atomic(manager.install_state_path, released_claim)
    released_bytes = manager.install_state_path.read_bytes()

    projected = manager.install_state()

    assert projected is not None
    assert projected["target"] == RUNTIME_INSTALL_TARGET
    assert manager.install_state_path.read_bytes() == released_bytes

    assert manager.transition_install_claim(
        InstallClaimTransition.RESUME,
        generation=RUNTIME_INSTALL_GENERATION_B,
        previous_generation=RUNTIME_INSTALL_GENERATION_A,
        target=RUNTIME_INSTALL_TARGET,
    )
    persisted = json.loads(manager.install_state_path.read_text(encoding="utf-8"))
    assert persisted == {
        "schema_version": 3,
        "state": "installing",
        "generation": RUNTIME_INSTALL_GENERATION_B,
        "error_key": None,
        "target": RUNTIME_INSTALL_TARGET,
    }


def test_stream_prelude_replays_large_keepalive_history_before_output() -> None:
    keepalive = b": " + b"k" * (64 * 1024) + b"\n\n"
    first = keepalive * 5
    output = b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'

    class Content:
        async def read(self, _size: int) -> bytes:
            return output

    response = SimpleNamespace(content=Content(), status=200)
    source = SourceRecord(
        source_id="src_keepalive1",
        vendor="anthropic",
        protocol="anthropic",
        base_url="https://example.test",
        credential_ref="cred_keepalive1",
        allowed_origins=("codex",),
        model_ids=("claude-sonnet-4-5",),
        prefix="keepalive",
    )

    async def run() -> tuple[bytes, object, object]:
        prelude = client_module._StreamPrelude()
        state = client_module.ProtocolSSEState("anthropic")
        outcome = await client_module._read_stream_prelude(
            response=response,
            first=first,
            prelude=prelude,
            wire_state=state,
            source=source,
            model_id="claude-sonnet-4-5",
            timeout=1,
        )
        payload = b"".join([chunk async for chunk in prelude.chunks()])
        prelude.close()
        return payload, state, outcome

    payload, state, outcome = asyncio.run(run())

    assert payload == first + output
    assert state.model_output_started is True
    assert outcome is None


def test_a_prelude_that_dies_after_reporting_tokens_carries_them_to_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MH-USAGE-005: usage reported before model output survives the failure.

    Anthropic bills input tokens on `message_start`, which arrives while the
    prelude is still buffering. A read that then times out never hands a body
    onward, so the resolver is the only half of metering that will ever see this
    call — and it can only see what the returned outcome carries.
    """

    async def run() -> None:
        message_start = (
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":900,"cache_read_input_tokens":128}}}\n\n'
        )
        reads = iter([message_start])

        class Content:
            async def read(self, _size: int) -> bytes:
                try:
                    return next(reads)
                except StopIteration:
                    raise asyncio.TimeoutError from None

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            content = Content()

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        source = SourceRecord(
            source_id="src_billedhalt",
            vendor="anthropic",
            protocol="anthropic",
            base_url="https://billed.example.test",
            credential_ref="cred_billedhalt",
            allowed_origins=("codex",),
            model_ids=("claude-sonnet-4-5",),
            prefix="billed",
        )
        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        client = EngineClient(
            EngineConnection("http://127.0.0.1:15221", "management", "gateway")
        )

        handle = await client.invoke(source, "claude-sonnet-4-5", {}, stream=True)
        outcome = await handle.outcome()

        assert handle.stream is None
        assert outcome.kind == RawOutcomeKind.TIMEOUT
        assert outcome.usage == ProtocolUsageReport(input_tokens=1028, cached_input_tokens=128)

    asyncio.run(run())


def test_stream_prelude_has_no_total_ceiling_and_cleans_spill() -> None:
    async def run() -> bytes:
        prelude = client_module._StreamPrelude(memory_limit=64)
        payload = b"x" * (2 * 1024 * 1024)
        prelude.write(payload)

        assert prelude.spilled is True
        assert prelude.stored_bytes == len(payload)
        replayed = b"".join([chunk async for chunk in prelude.chunks()])
        prelude.close()
        assert prelude.closed is True
        return replayed

    assert asyncio.run(run()) == b"x" * (2 * 1024 * 1024)


def test_engine_json_responses_are_read_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = io.BytesIO(body)

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert size == client_module._STREAM_CHUNK_BYTES
            return self.body.read(min(size, 31))

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    payload = {
        "state": "ok",
        "large_integer": 123456789012345678901234567890,
        "ratio": 1.25,
        "metadata": "x" * (2 * 1024 * 1024),
    }
    response = Response(json.dumps(payload).encode())
    monkeypatch.setattr(
        client_module.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=lambda *_args, **_kwargs: response),
    )
    client = EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway"))

    assert client.management_request("GET", "/fixture") == payload
    assert reads and all(size == client_module._STREAM_CHUNK_BYTES for size in reads)


def test_deadline_reader_falls_back_when_reader_has_no_readinto() -> None:
    class Reader:
        def __init__(self) -> None:
            self.body = io.BytesIO(b"fixture")

        def read(self, size: int = -1) -> bytes:
            return self.body.read(size)

    buffer = bytearray(4)
    reader = client_module._DeadlineReader(Reader(), time.monotonic() + 1)

    assert reader.readinto(buffer) == 4
    assert bytes(buffer) == b"fixt"


def test_engine_json_response_spooling_uses_one_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0
    socket_timeouts: list[float] = []

    class ResponseSocket:
        def settimeout(self, timeout: float) -> None:
            socket_timeouts.append(timeout)

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=ResponseSocket()))

        def read(self, size: int = -1) -> bytes:
            nonlocal reads
            assert size == client_module._STREAM_CHUNK_BYTES
            reads += 1
            return b" "

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    ticks = iter((10.0, 10.1, 10.6, 11.1))
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        client_module.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=lambda *_args, **_kwargs: Response()),
    )
    client = EngineClient(
        EngineConnection("http://127.0.0.1:15220", "management", "gateway"),
        timeout=1.0,
    )

    with pytest.raises(EngineClientError) as caught:
        client.management_request("GET", "/fixture")

    assert caught.value.error_type == "TimeoutError"
    assert reads == 2
    assert socket_timeouts == pytest.approx([0.9, 0.4])


def test_engine_json_projection_uses_the_request_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self) -> None:
            self.body = io.BytesIO(b"{}")

        def read(self, size: int = -1) -> bytes:
            assert size == client_module._STREAM_CHUNK_BYTES
            return self.body.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    ticks = iter((10.0, 10.1, 10.2, 10.3, 10.4, 11.1))
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        client_module.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=lambda *_args, **_kwargs: Response()),
    )
    client = EngineClient(
        EngineConnection("http://127.0.0.1:15220", "management", "gateway"),
        timeout=1.0,
    )

    def projector(reader) -> bool:
        assert reader.read(1) == b"{"
        reader.read(1)
        return True

    with pytest.raises(EngineClientError) as caught:
        client._request_json_projection("GET", "/fixture", projector)

    assert caught.value.error_type == "TimeoutError"


def test_engine_health_projects_only_required_facts_from_large_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = io.BytesIO(body)

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert size == client_module._STREAM_CHUNK_BYTES
            return self.body.read(min(size, 37))

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def open_response(request, **_kwargs):
        if request.full_url.endswith("/v1/models"):
            return Response(
                json.dumps(
                    {
                        "object": "list",
                        "data": [{"id": "model", "metadata": "x" * (2 * 1024 * 1024)}],
                    }
                ).encode()
            )
        return Response(json.dumps({"sources": ["x" * (2 * 1024 * 1024)]}).encode())

    monkeypatch.setattr(
        client_module.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=open_response),
    )
    client = EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway"))

    assert client.health() is True
    assert reads and all(size == client_module._STREAM_CHUNK_BYTES for size in reads)


def test_engine_error_projection_does_not_materialize_unrelated_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[int] = []

    class ErrorBody(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert size == client_module._STREAM_CHUNK_BYTES
            return super().read(min(size, 41))

    body = ErrorBody(
        json.dumps(
            {
                "error": {"type": "permission_error", "code": "permission_error"},
                "metadata": "x" * (2 * 1024 * 1024),
            }
        ).encode()
    )
    error = client_module.urllib.error.HTTPError(
        "http://127.0.0.1:15220/v0/management/fixture",
        403,
        "Forbidden",
        {},
        body,
    )
    monkeypatch.setattr(
        client_module.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error)),
    )
    client = EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway"))

    with pytest.raises(EngineClientError) as caught:
        client.management_request("GET", "/fixture")

    assert caught.value.status_code == 403
    assert caught.value.error_type == "permission_error"
    assert caught.value.error_code == "permission_error"
    assert reads and all(size == client_module._STREAM_CHUNK_BYTES for size in reads)


def test_stream_prelude_uses_one_absolute_pre_output_deadline() -> None:
    async def run() -> None:
        class Content:
            async def read(self, _size: int) -> bytes:
                await asyncio.sleep(0.02)
                return b": keepalive\n\n"

        response = SimpleNamespace(content=Content(), status=200)
        source = SourceRecord(
            source_id="src_deadline1",
            vendor="anthropic",
            protocol="anthropic",
            base_url="https://example.test",
            credential_ref="cred_deadline1",
            allowed_origins=("codex",),
            model_ids=("claude-sonnet-4-5",),
            prefix="deadline",
        )
        prelude = client_module._StreamPrelude(memory_limit=64)
        state = client_module.ProtocolSSEState("anthropic")

        with pytest.raises(asyncio.TimeoutError):
            await client_module._read_stream_prelude(
                response=response,
                first=b": first\n\n",
                prelude=prelude,
                wire_state=state,
                source=source,
                model_id="claude-sonnet-4-5",
                timeout=0.01,
            )
        prelude.close()

    asyncio.run(run())


def test_usage_is_observed_and_replayed_after_prelude_spill() -> None:
    message_start = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":900,"cache_read_input_tokens":128}}}\n\n'
    )
    filler = b": " + b"k" * 4090 + b"\n\n"
    prelude = client_module._StreamPrelude(memory_limit=64)
    state = client_module.ProtocolSSEState("anthropic")

    async def replay() -> bytes:
        await client_module._received(filler, prelude=prelude, wire_state=state)
        await client_module._received(message_start, prelude=prelude, wire_state=state)
        return b"".join([chunk async for chunk in prelude.chunks()])

    assert asyncio.run(replay()) == filler + message_start
    assert prelude.spilled is True
    assert state.usage == ProtocolUsageReport(input_tokens=1028, cached_input_tokens=128)
    prelude.close()


def _write_fixture_archive(tmp_path: Path, *, version: str = "7.2.95") -> tuple[Path, bytes]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = (
        f'#!/bin/sh\nif [ "$1" = "--help" ]; then\n  echo \'CLIProxyAPI Version: {version}, Commit: fixture\' >&2\nfi\n'
    ).encode()
    archive = tmp_path / "CLIProxyAPI_fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("cli-proxy-api")
        member.mode = 0o755
        member.size = len(binary)
        tar.addfile(member, io.BytesIO(binary))
    return archive, binary


def _write_fixture_manifest(
    tmp_path: Path,
    archive: Path,
    binary: bytes,
    *,
    archive_sha256: str | None = None,
) -> Path:
    platform_tag = managed_runtime.runtime_platform_tag()
    manifest_platform = "linux-amd64" if platform_tag == "linux-x64" else platform_tag
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "cliproxyapi",
                "version": "v7.2.95",
                "source": "router-for-me/CLIProxyAPI",
                "source_url": "https://example.test/source",
                "source_sha": "f71ec0eb6776854457892452cf28c47f0d658251",
                "release_tag": "v7.2.95",
                "license": "MIT",
                "assets": [
                    {
                        "platform": manifest_platform,
                        "url": archive.as_uri(),
                        "size_bytes": archive.stat().st_size,
                        "sha256": archive_sha256 or hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "binary_sha256": hashlib.sha256(binary).hexdigest(),
                        "bin_path": "cli-proxy-api",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def _binding(credential_ref: str, **overrides: object) -> SourceBinding:
    payload = {
        "source_id": "src_fixture123",
        "vendor": "custom",
        "protocol": "openai_chat",
        "base_url": "https://api.example.test/v1",
        "credential_ref": credential_ref,
        "allowed_origins": (),
        "model_ids": ("model-a",),
    }
    payload.update(overrides)
    return SourceBinding(**payload)  # type: ignore[arg-type]


def test_oauth_retained_material_vocabulary_and_pairing_guard() -> None:
    assert {item.value for item in RetainedMaterialDisposition} == {
        "none",
        "flow_source_ref",
        "orphan_ref",
        "foreign_source_ref",
        "unknown",
    }
    flow = SimpleNamespace(
        retained_material_disposition=RetainedMaterialDisposition.NONE,
        retained_credential_ref=None,
    )

    for disposition, credential_ref in (
        (RetainedMaterialDisposition.NONE, None),
        (RetainedMaterialDisposition.FLOW_SOURCE_REF, "cred_flow"),
        (RetainedMaterialDisposition.ORPHAN_REF, "cred_orphan"),
        (RetainedMaterialDisposition.FOREIGN_SOURCE_REF, None),
        (RetainedMaterialDisposition.UNKNOWN, None),
    ):
        CLIProxyEngineAdapter._set_retained_material(  # type: ignore[arg-type]
            flow,
            disposition,
            credential_ref,
        )

    for disposition, credential_ref in (
        (RetainedMaterialDisposition.NONE, "cred_invalid"),
        (RetainedMaterialDisposition.FLOW_SOURCE_REF, None),
        (RetainedMaterialDisposition.ORPHAN_REF, None),
        (RetainedMaterialDisposition.FOREIGN_SOURCE_REF, "cred_invalid"),
        (RetainedMaterialDisposition.UNKNOWN, "cred_invalid"),
    ):
        with pytest.raises(AssertionError, match="pairing"):
            CLIProxyEngineAdapter._set_retained_material(  # type: ignore[arg-type]
                flow,
                disposition,
                credential_ref,
            )


def test_orphaned_oauth_cleanup_keeps_ref_until_deletes_are_confirmed(
    tmp_path: Path,
) -> None:
    class Client:
        fail_delete = True

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            assert (method, path) == ("DELETE", "/auth-files")
            if self.fail_delete:
                raise EngineClientError("delete failed")
            return {"status": "ok"}

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client_if_running(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        auth_file = store.auth_dir / "claude-account.json"
        auth_file.write_text("{}", encoding="utf-8")
        auth_file.chmod(0o600)
        credential_ref = store.bind_oauth_credential(
            "src_fixture123",
            "anthropic",
            auth_file.name,
        )
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, client),  # type: ignore[arg-type]
            state_store=store,
        )

        assert await adapter.cleanup_orphaned_oauth_material(credential_ref) is False
        assert store.credential_metadata(credential_ref)["auth_name"] == auth_file.name

        client.fail_delete = False
        assert await adapter.cleanup_orphaned_oauth_material(credential_ref) is True
        with pytest.raises(EngineStateError, match="unavailable"):
            store.credential_metadata(credential_ref)

    asyncio.run(run())


def test_orphaned_oauth_cleanup_retry_converges_after_journal_crash(
    tmp_path: Path,
) -> None:
    class Client:
        delete_calls = 0

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            assert (method, path) == ("DELETE", "/auth-files")
            self.delete_calls += 1
            return {"status": "ok"}

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client_if_running(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        auth_file = store.auth_dir / "claude-account.json"
        auth_file.write_text("{}", encoding="utf-8")
        auth_file.chmod(0o600)
        credential_ref = store.bind_oauth_credential(
            "src_fixture123",
            "anthropic",
            auth_file.name,
        )
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, client),  # type: ignore[arg-type]
            state_store=store,
        )
        cleanup_journal = {credential_ref}

        assert await adapter.cleanup_orphaned_oauth_material(credential_ref) is True
        assert credential_ref in cleanup_journal  # Crash before journal clear.
        assert await adapter.cleanup_orphaned_oauth_material(credential_ref) is True
        cleanup_journal.remove(credential_ref)

        assert cleanup_journal == set()
        assert client.delete_calls == 1

    asyncio.run(run())


def test_orphaned_oauth_cleanup_never_existed_ref_is_converged(
    tmp_path: Path,
) -> None:
    class Supervisor:
        def client_if_running(self):
            raise AssertionError("an absent ref must not reach the engine")

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(),  # type: ignore[arg-type]
            state_store=store,
        )

        assert await adapter.cleanup_orphaned_oauth_material("cred_00000000000000000000000000000000") is True

    asyncio.run(run())


def test_packaged_manifest_matches_frozen_runtime_dependency_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)

    manifest = manager.contract_manifest()

    assert manifest == {
        "name": "cliproxyapi",
        "resolution": "resolved",
        "version": "v7.2.105",
        "source_sha": "4a2eb54dc6bf943196be4fb515e6a9407a4db143",
        "assets": [
            {
                "platform": "darwin-arm64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.105/CLIProxyAPI_7.2.105_darwin_aarch64.tar.gz",
                "size_bytes": 18975205,
                "sha256": "641de855c486d373b3c69704bec55a5c5ce3efa523149cc9bd253f76040470d7",
            },
            {
                "platform": "darwin-x64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.105/CLIProxyAPI_7.2.105_darwin_amd64.tar.gz",
                "size_bytes": 20513376,
                "sha256": "c9332b8401cd54d357e7c66e88bce603fdb497701a7fa86ee2f82bb1aad846b9",
            },
            {
                "platform": "linux-amd64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.105/CLIProxyAPI_7.2.105_linux_amd64.tar.gz",
                "size_bytes": 20558559,
                "sha256": "f432872815fe85ac4b0f83b5598253725eea70aae4c95025194cf558f6acef31",
            },
            {
                "platform": "linux-arm64",
                "url": "https://github.com/router-for-me/CLIProxyAPI/releases/download/v7.2.105/CLIProxyAPI_7.2.105_linux_aarch64.tar.gz",
                "size_bytes": 18559648,
                "sha256": "b72245cf1958251330eae9e17f1fc5a077f94146b2eea30e23ab5012c6059981",
            },
        ],
    }

    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "win32-x64")
    unsupported = EngineRuntimeManager(
        runtime_dir=tmp_path / "unsupported-runtime",
        offline=True,
    ).ensure()
    assert unsupported["ok"] is False
    assert unsupported["reason"] == "model_hub_engine_platform_unsupported"


def test_contract_manifest_filters_unsupported_override_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(Path("vibe/model_hub_runtime/cliproxyapi_manifest.json").read_text(encoding="utf-8"))
    payload["assets"].append(
        {
            "platform": "win32-x64",
            "url": "https://example.test/unsupported.zip",
            "size_bytes": 1,
            "sha256": "1" * 64,
            "binary_sha256": "2" * 64,
            "bin_path": "cli-proxy-api.exe",
        }
    )
    manifest_path = tmp_path / "override.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manager = EngineRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        manifest_path=manifest_path,
        offline=True,
    )

    assert "win32-x64" not in {asset["platform"] for asset in manager.contract_manifest()["assets"]}

    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "win32-x64")
    unsupported = manager.ensure()
    assert unsupported["ok"] is False
    assert unsupported["reason"] == "model_hub_engine_platform_unsupported"


def test_install_admission_fetches_an_uncached_remote_manifest(tmp_path: Path) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "remote")
    manifest = _write_fixture_manifest(tmp_path / "remote", archive, binary)
    manager = EngineRuntimeManager(
        runtime_dir=tmp_path / "runtime",
        manifest_url=manifest.as_uri(),
    )

    assert manager.contract_manifest() == {
        "name": "cliproxyapi",
        "resolution": "unresolved",
        "assets": [],
    }
    installed = manager.ensure(
        on_resolved=lambda target: manager.transition_install_claim(
            InstallClaimTransition.CREATE,
            generation=RUNTIME_INSTALL_GENERATION_A,
            target=target,
        )
    )

    persisted = manager.install_state()
    assert installed["ok"] is True
    assert persisted is not None
    assert persisted["state"] == "installing"
    assert persisted["target"] == installed["target"]
    assert persisted["target"]["platform"] == manager.host_platform()
    assert manager.contract_manifest()["assets"]


def test_released_claim_replay_ignores_unrelated_manifest_entry_without_reinstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "fixture")
    manifest_path = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()
    assert installed["ok"] is True
    original_manifest = manager._load_manifest(allow_network=False)
    assert original_manifest is not None
    managed_runtime.write_json_atomic(
        manager.install_state_path,
        {
            "schema_version": 2,
            "state": "installing",
            "generation": RUNTIME_INSTALL_GENERATION_A,
            "error_key": None,
            "target": {
                "manifest_sha256": original_manifest.digest,
                **installed["target"],
            },
        },
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"].append(
        {
            "platform": "fixture-unrelated",
            "url": "https://example.test/unrelated.tar.gz",
            "size_bytes": 1,
            "sha256": "d" * 64,
            "binary_sha256": "e" * 64,
            "bin_path": "unused",
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    pointer_path = manager.runtime_dir / "current.json"
    metadata_path = Path(installed["install_dir"]) / manager.spec.metadata_filename
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("released claim replay accessed an archive"),
    )
    monkeypatch.setattr(
        manager,
        "_write_current_pointer",
        lambda *_args: pytest.fail("released claim replay rewrote the pointer"),
    )

    claim = manager.install_state()
    assert claim is not None
    replayed = manager.ensure(expected_target=claim["target"])

    assert claim["target"] == installed["target"]
    assert replayed["ok"] is True
    assert replayed["changed"] is False
    assert replayed["path"] == installed["path"]
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before


def test_released_linux_x64_pointer_and_claim_are_admitted_by_platform_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "linux-x64")
    archive, binary = _write_fixture_archive(tmp_path / "fixture")
    manifest_path = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()
    assert installed["ok"] is True
    assert installed["target"]["platform"] == "linux-amd64"
    original_manifest = manager._load_manifest(allow_network=False)
    assert original_manifest is not None

    pointer_path = manager.runtime_dir / "current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    canonical_install_dir = Path(pointer["install_dir"])
    alias_platform_dir = canonical_install_dir.parent.with_name("linux-x64")
    canonical_install_dir.parent.rename(alias_platform_dir)
    alias_install_dir = alias_platform_dir / canonical_install_dir.name
    pointer["platform"] = "linux-x64"
    pointer["install_dir"] = str(alias_install_dir)
    managed_runtime.write_json_atomic(pointer_path, pointer)
    metadata_path = alias_install_dir / manager.spec.metadata_filename
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["platform"] = "linux-x64"
    managed_runtime.write_json_atomic(metadata_path, metadata)
    managed_runtime.write_json_atomic(
        manager.install_state_path,
        {
            "schema_version": 2,
            "state": "installing",
            "generation": RUNTIME_INSTALL_GENERATION_A,
            "error_key": None,
            "target": {
                "manifest_sha256": original_manifest.digest,
                **installed["target"],
                "platform": "linux-x64",
            },
        },
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"].append(
        {
            "platform": "fixture-unrelated",
            "url": "https://example.test/unrelated.tar.gz",
            "size_bytes": 1,
            "sha256": "d" * 64,
            "binary_sha256": "e" * 64,
            "bin_path": "unused",
        }
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    pointer_before = pointer_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    monkeypatch.setattr(
        manager,
        "_resolve_manifest_archive",
        lambda _archive: pytest.fail("alias-equivalent replay accessed an archive"),
    )
    monkeypatch.setattr(
        manager,
        "_write_current_pointer",
        lambda *_args: pytest.fail("alias-equivalent replay rewrote the pointer"),
    )

    status = manager.status()
    claim = manager.install_state()
    assert claim is not None
    replayed = manager.ensure(expected_target=claim["target"])

    assert status["installed"] is True
    assert status["version"] == "v7.2.95"
    assert status["selected_version"] == "v7.2.95"
    assert status["matches_manifest"] is True
    assert status["path"] == str(alias_install_dir / "cli-proxy-api")
    assert manager.resolve_engine_path() == alias_install_dir / "cli-proxy-api"
    assert claim["target"] == installed["target"]
    assert replayed["ok"] is True
    assert replayed["changed"] is False
    assert replayed["path"] == str(alias_install_dir / "cli-proxy-api")
    assert pointer_path.read_bytes() == pointer_before
    assert metadata_path.read_bytes() == metadata_before


@pytest.mark.parametrize("transition", tuple(InstallClaimTransition), ids=lambda item: item.value)
def test_every_install_claim_transition_preserves_live_generation_ownership(
    tmp_path: Path,
    transition: InstallClaimTransition,
) -> None:
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    live_generations: set[str]

    if transition is InstallClaimTransition.CREATE:
        applied = manager.transition_install_claim(
            transition,
            generation=RUNTIME_INSTALL_GENERATION_B,
            target=RUNTIME_INSTALL_TARGET,
        )
        live_generations = {RUNTIME_INSTALL_GENERATION_B}
    elif transition is InstallClaimTransition.ADMISSION_FAILURE:
        applied = manager.transition_install_claim(
            transition,
            generation=RUNTIME_INSTALL_GENERATION_B,
            reason="fixture_admission_failure",
        )
        live_generations = set()
    else:
        _create_runtime_install_claim(manager)
        if transition is InstallClaimTransition.RESUME:
            applied = manager.transition_install_claim(
                transition,
                generation=RUNTIME_INSTALL_GENERATION_B,
                previous_generation=RUNTIME_INSTALL_GENERATION_A,
                target=RUNTIME_INSTALL_TARGET,
            )
            live_generations = {RUNTIME_INSTALL_GENERATION_B}
        elif transition is InstallClaimTransition.SETTLE_SUCCESS:
            applied = manager.transition_install_claim(
                transition,
                generation=RUNTIME_INSTALL_GENERATION_A,
                target=RUNTIME_INSTALL_TARGET,
            )
            live_generations = set()
        elif transition in {
            InstallClaimTransition.SETTLE_FAILURE,
            InstallClaimTransition.ABANDON,
        }:
            applied = manager.transition_install_claim(
                transition,
                generation=RUNTIME_INSTALL_GENERATION_A,
                target=RUNTIME_INSTALL_TARGET,
                reason=f"fixture_{transition.value}",
            )
            live_generations = set()
        else:
            raise AssertionError(f"unmodelled install claim transition: {transition}")

    assert applied is True
    state = manager.install_state()
    if state is not None and state["state"] == "installing":
        assert state["generation"] in live_generations
    else:
        assert not live_generations
        if transition is not InstallClaimTransition.SETTLE_SUCCESS:
            assert state is not None
            assert state["state"] == "not_installed"
            assert state["error_key"] == "settings.models.install.fail.detail"


@pytest.mark.parametrize(
    "stale_settlement",
    tuple(
        transition
        for transition in InstallClaimTransition
        if transition
        not in {InstallClaimTransition.CREATE, InstallClaimTransition.RESUME}
    ),
    ids=lambda item: item.value,
)
def test_new_owner_claim_survives_every_stale_settlement(
    tmp_path: Path,
    stale_settlement: InstallClaimTransition,
) -> None:
    owner_a = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    owner_b = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    _create_runtime_install_claim(owner_a)
    assert owner_b.transition_install_claim(
        InstallClaimTransition.RESUME,
        generation=RUNTIME_INSTALL_GENERATION_B,
        previous_generation=RUNTIME_INSTALL_GENERATION_A,
        target=RUNTIME_INSTALL_TARGET,
    )

    kwargs = {}
    if stale_settlement is not InstallClaimTransition.SETTLE_SUCCESS:
        kwargs["reason"] = "fixture_stale_owner"
    target = (
        None
        if stale_settlement is InstallClaimTransition.ADMISSION_FAILURE
        else RUNTIME_INSTALL_TARGET
    )
    assert owner_a.transition_install_claim(
        stale_settlement,
        generation=RUNTIME_INSTALL_GENERATION_A,
        target=target,
        **kwargs,
    ) is False

    surviving = owner_b.install_state()
    assert surviving is not None
    assert surviving["state"] == "installing"
    assert surviving["generation"] == RUNTIME_INSTALL_GENERATION_B
    assert surviving["target"] == RUNTIME_INSTALL_TARGET


@pytest.mark.parametrize("resolution", tuple(ManifestResolution), ids=lambda item: item.value)
def test_manifest_resolution_drives_admission_persistence_and_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolution: ManifestResolution,
) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "fixture")
    manifest_path = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
    if resolution is ManifestResolution.UNRESOLVED:
        manager = EngineRuntimeManager(
            runtime_dir=tmp_path / "runtime",
            manifest_url=manifest_path.as_uri(),
        )
    else:
        if resolution is ManifestResolution.UNSUPPORTED:
            monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "win32-x64")
        elif resolution is not ManifestResolution.RESOLVED:
            raise AssertionError(f"unmodelled manifest resolution: {resolution}")
        manager = EngineRuntimeManager(
            runtime_dir=tmp_path / "runtime",
            manifest_path=manifest_path,
            offline=False,
        )

    supervisor = EngineSupervisor(
        installer=manager,
        state_store=EngineStateStore(tmp_path / "state"),
    )
    projected = {"contract_version": 8, **supervisor.status()}
    schema = json.loads(
        Path("docs/plans/model-hub-contracts/runtime-dependency.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft7Validator(schema).validate(projected)
    assert projected["manifest"]["resolution"] == resolution.value

    claim_calls = 0

    def persist_claim(target: dict[str, str]) -> None:
        nonlocal claim_calls
        claim_calls += 1
        assert manager.transition_install_claim(
            InstallClaimTransition.CREATE,
            generation=RUNTIME_INSTALL_GENERATION_A,
            target=target,
        )

    admitted = manager.ensure(on_resolved=persist_claim)
    state = manager.install_state()
    if resolution is ManifestResolution.UNSUPPORTED:
        assert admitted["ok"] is False
        assert admitted["reason"] == "model_hub_engine_platform_unsupported"
        assert claim_calls == 0
        assert state is None
        assert projected["status"]["health"] == "not_installed"
        assert projected["status"]["error_key"] is None
    else:
        assert admitted["ok"] is True
        assert claim_calls == 1
        assert state is not None
        assert state["state"] == "installing"
        assert state["error_key"] is None


def test_every_runtime_install_failure_reason_has_a_non_collapsing_mapping(
    tmp_path: Path,
) -> None:
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)

    for reason in manager.install_failure_reasons():
        error = CLIProxyEngineAdapter._install_failure(reason)
        if reason == "model_hub_engine_platform_unsupported":
            assert isinstance(error, RuntimePlatformUnsupportedError)
        else:
            assert isinstance(error, EngineUnavailableError)
            assert error.reason == reason


@pytest.mark.parametrize(
    ("live_owner", "resumable_claim"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_installing_projection_matches_live_owner_or_resumable_claim(
    tmp_path: Path,
    live_owner: bool,
    resumable_claim: bool,
) -> None:
    async def run() -> None:
        installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
        if resumable_claim:
            _create_runtime_install_claim(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )
        adapter._install_owner_active = live_owner

        status = await adapter.status()

        assert (status.health is EngineHealth.INSTALLING) is (
            live_owner or resumable_claim
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("host_platform", "asset_platform", "size_bytes", "archive_sha256", "binary_sha256"),
    [
        (
            "darwin-arm64",
            "darwin-arm64",
            18975205,
            "641de855c486d373b3c69704bec55a5c5ce3efa523149cc9bd253f76040470d7",
            "e8da44e6bf9d85fe7b98a2843d33ff509156727222d6a1ad5dd1a79709849337",
        ),
        (
            "darwin-x64",
            "darwin-x64",
            20513376,
            "c9332b8401cd54d357e7c66e88bce603fdb497701a7fa86ee2f82bb1aad846b9",
            "07a607965a40f782f63625c557eeeddb39b08e08b1a3057881bb13fe6e887109",
        ),
        (
            "linux-x64",
            "linux-amd64",
            20558559,
            "f432872815fe85ac4b0f83b5598253725eea70aae4c95025194cf558f6acef31",
            "2717656b33a0d76a7c02b451797341e8791f740a72d4e71577140886f42ba628",
        ),
        (
            "linux-arm64",
            "linux-arm64",
            18559648,
            "b72245cf1958251330eae9e17f1fc5a077f94146b2eea30e23ab5012c6059981",
            "8c389d565b8555d5788314e56258c64d675ff76bb9e32047d339088f2789b07e",
        ),
    ],
)
def test_engine_installer_selects_verified_packaged_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_platform: str,
    asset_platform: str,
    size_bytes: int,
    archive_sha256: str,
    binary_sha256: str,
) -> None:
    monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: host_platform)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / host_platform, offline=True)

    manifest = manager._load_manifest(allow_network=False)

    assert manifest is not None
    archive = manager._manifest_archive_for_platform(manifest)
    assert archive is not None
    assert archive.platform == asset_platform
    assert archive.size == size_bytes
    assert archive.sha256 == archive_sha256
    assert archive.binary_sha256 == binary_sha256


def test_engine_platform_identity_is_normalized_locally() -> None:
    assert dict(runtime_installer_module._ENGINE_SPEC.platform_aliases) == runtime_installer_module._ENGINE_PLATFORM_MAP
    assert EngineRuntimeManager._normalize_engine_platform("linux-x64") == "linux-amd64"
    assert EngineRuntimeManager._normalize_engine_platform("linux-amd64") == "linux-amd64"


def test_engine_installer_is_idempotent_and_rejects_tampered_archive(tmp_path: Path) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "good")
    manifest = _write_fixture_manifest(tmp_path / "good", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    first = manager.ensure()
    second = manager.ensure()

    assert first["ok"] is True
    assert first["changed"] is True
    assert second["ok"] is True
    assert second["changed"] is False
    assert manager.status()["installed"] is True

    bad_archive, bad_binary = _write_fixture_archive(tmp_path / "bad")
    bad_manifest = _write_fixture_manifest(
        tmp_path / "bad",
        bad_archive,
        bad_binary,
        archive_sha256="1" * 64,
    )
    rejected = EngineRuntimeManager(
        runtime_dir=tmp_path / "bad-runtime",
        manifest_path=bad_manifest,
    ).ensure()
    assert rejected["ok"] is False
    assert rejected["reason"] == "model_hub_engine_archive_checksum_mismatch"


def test_engine_status_rehashes_binary_only_after_file_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, binary = _write_fixture_archive(tmp_path / "fixture")
    manifest = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    installed = manager.ensure()
    binary_path = Path(installed["path"])
    original_file_sha256 = managed_runtime.file_sha256
    hashed_paths: list[Path] = []

    def tracked_file_sha256(path: Path) -> str:
        hashed_paths.append(path)
        return original_file_sha256(path)

    monkeypatch.setattr(managed_runtime, "file_sha256", tracked_file_sha256)

    assert manager.status()["installed"] is True
    assert manager.status()["installed"] is True
    assert hashed_paths == [binary_path]

    binary_path.write_bytes(binary_path.read_bytes() + b"\n# tampered\n")

    assert manager.status()["installed"] is False
    assert hashed_paths == [binary_path, binary_path]


def test_engine_version_check_uses_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}

    def run(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "", "CLIProxyAPI Version: 7.2.95")

    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-github-secret")
    monkeypatch.setattr("vibe.model_hub_runtime.installer.subprocess.run", run)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)

    assert manager._binary_version(tmp_path / "cli-proxy-api") == "v7.2.95"
    assert "OPENAI_API_KEY" not in captured_env
    assert "GITHUB_TOKEN" not in captured_env
    assert captured_env == engine_subprocess_environment()


def test_config_generation_is_private_and_never_logs_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    store = EngineStateStore(tmp_path / "state")
    instance_dir, runtime_secrets = store.prepare_instance("install-1")
    credential_ref = store.store_api_key(
        "upstream-secret-value",
        base_url="https://api.example.test/v1",
    )
    responses_ref = store.store_api_key(
        "responses-secret-value",
        vendor="openai",
        protocol="openai_responses",
    )
    codex_ref = store.store_api_key(
        "codex-secret-value",
        vendor="codex",
        protocol="openai_responses",
    )
    deepseek_ref = store.store_api_key(
        "deepseek-secret-value",
        vendor="deepseek",
        protocol="openai_chat",
    )
    store.sync_sources(
        [
            _binding(credential_ref),
            _binding(
                responses_ref,
                source_id="src_responses1",
                vendor="openai",
                protocol="openai_responses",
                base_url=None,
                model_reasoning_efforts=(("model-a", ("low", "high")),),
            ),
            _binding(
                codex_ref,
                source_id="src_codexresp1",
                vendor="codex",
                protocol="openai_responses",
                base_url=None,
            ),
            _binding(
                deepseek_ref,
                source_id="src_deepseekcfg",
                vendor="deepseek",
                protocol="openai_chat",
                base_url=None,
            ),
        ]
    )
    config_path = instance_dir / "config.yaml"

    write_engine_config(
        config_path,
        host="127.0.0.1",
        port=18231,
        auth_dir=store.auth_dir,
        runtime_secrets=runtime_secrets,
        sources=store.list_sources(),
        state_store=store,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["host"] == "127.0.0.1"
    assert payload["usage-statistics-enabled"] is False
    assert payload["force-model-prefix"] is True
    assert payload["request-retry"] == 0
    assert payload["max-retry-credentials"] == 1
    assert payload["plugins"]["enabled"] is False
    assert payload["remote-management"]["allow-remote"] is False
    assert payload["remote-management"]["disable-control-panel"] is True
    assert payload["openai-compatibility"][0]["api-key-entries"][0]["api-key"] == ("upstream-secret-value")
    assert {
        entry["base-url"] for entry in payload["openai-compatibility"]
    } == {"https://api.example.test/v1", "https://api.deepseek.com"}
    assert len(payload["codex-api-key"]) == 2
    assert {entry["base-url"] for entry in payload["codex-api-key"]} == {"https://api.openai.com/v1"}
    responses_entry = next(
        entry
        for entry in payload["codex-api-key"]
        if entry["api-key"] == "responses-secret-value"
    )
    assert responses_entry["models"] == [
        {
            "name": "model-a",
            "alias": "model-a",
            "thinking": {"levels": ["high", "low"]},
        }
    ]
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.auth_dir.stat().st_mode) == 0o700
    credential_path = next((store.root / "credentials").iterdir())
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    for secret in (
        runtime_secrets.management_key,
        runtime_secrets.gateway_token,
        "upstream-secret-value",
        "responses-secret-value",
        "codex-secret-value",
        "deepseek-secret-value",
    ):
        assert secret not in caplog.text

    secrets_path = instance_dir / "runtime-secrets.json"
    secrets_path.chmod(0o644)
    with pytest.raises(EngineStateError, match="runtime secret permissions are unsafe"):
        store.prepare_instance("install-1")


def test_mixed_anthropic_credentials_disable_cloak_only_for_api_key_entry(
    tmp_path: Path,
) -> None:
    store = EngineStateStore(tmp_path / "state")
    instance_dir, runtime_secrets = store.prepare_instance("install-1")
    api_key_ref = store.store_api_key(
        "api-key-fixture",
        vendor="anthropic",
        protocol="anthropic",
    )
    oauth_auth_name = "claude-oauth.json"
    oauth_ref = store.bind_oauth_credential(
        "src_oauth0001",
        "anthropic",
        oauth_auth_name,
    )
    oauth_path = store.auth_dir / oauth_auth_name
    oauth_content = b'{"type":"claude","access_token":"oauth-fixture"}\n'
    oauth_path.write_bytes(oauth_content)
    oauth_path.chmod(0o600)
    store.sync_sources(
        [
            _binding(
                api_key_ref,
                source_id="src_apikey001",
                vendor="anthropic",
                protocol="anthropic",
                base_url=None,
                model_ids=("claude-api-model",),
            ),
            _binding(
                oauth_ref,
                source_id="src_oauth0001",
                vendor="anthropic",
                protocol="anthropic",
                base_url=None,
                allowed_origins=("claude",),
                model_ids=("claude-oauth-model",),
            ),
        ]
    )

    config_path = instance_dir / "config.yaml"
    write_engine_config(
        config_path,
        host="127.0.0.1",
        port=18231,
        auth_dir=store.auth_dir,
        runtime_secrets=runtime_secrets,
        sources=store.list_sources(),
        state_store=store,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert payload["disable-claude-cloak-mode"] is True
    assert payload["claude-api-key"] == [
        {
            "api-key": "api-key-fixture",
            "prefix": store.get_source("src_apikey001").prefix,
            "base-url": "https://api.anthropic.com",
            "cloak": {"mode": "never"},
            "rebuild-mid-system-message": False,
            "models": [
                {"name": "claude-api-model", "alias": "claude-api-model"}
            ],
        }
    ]
    assert oauth_path.read_bytes() == oauth_content
    assert "cloak" not in store.credential_metadata(oauth_ref)
    assert "rebuild-mid-system-message" not in store.credential_metadata(oauth_ref)


def test_state_rejects_unsafe_inputs_and_auth_permissions(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    store.prepare_instance("install-1")
    credential_ref = store.store_api_key(
        "secret",
        base_url="https://api.example.test/v1",
    )

    with pytest.raises(EngineStateError, match="invalid source base URL"):
        store.sync_sources([_binding(credential_ref, base_url="https://user:password@example.test/v1")])

    incomplete_ref = store.store_api_key("secret", base_url=None)
    with pytest.raises(EngineStateError, match="requires a base URL"):
        store.sync_sources([_binding(incomplete_ref, base_url=None)])

    custom_anthropic_ref = store.store_api_key(
        "secret",
        vendor="custom",
        protocol="anthropic",
        base_url=None,
    )
    with pytest.raises(EngineStateError, match="requires a base URL"):
        store.sync_sources(
            [
                _binding(
                    custom_anthropic_ref,
                    vendor="custom",
                    protocol="anthropic",
                    base_url=None,
                )
            ]
        )

    with pytest.raises(EngineStateError, match="at least one model"):
        store.sync_sources([_binding(credential_ref, model_ids=())])

    official_anthropic_ref = store.store_api_key(
        "secret",
        vendor="anthropic",
        protocol="anthropic",
        base_url=None,
    )
    official = store.sync_sources(
        [
            _binding(
                official_anthropic_ref,
                vendor="anthropic",
                protocol="anthropic",
                base_url=None,
            )
        ]
    )
    assert official[0].base_url is None

    official_deepseek_ref = store.store_api_key(
        "secret",
        vendor="deepseek",
        protocol="openai_chat",
        base_url=None,
    )
    official_deepseek = store.sync_sources(
        [
            _binding(
                official_deepseek_ref,
                source_id="src_deepseek01",
                vendor="deepseek",
                protocol="openai_chat",
                base_url=None,
            )
        ]
    )
    assert official_deepseek[0].base_url is None

    auth_file = store.auth_dir / "oauth.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o644)
    with pytest.raises(EngineStateError, match="credential permissions are unsafe"):
        store.audit_auth_permissions()
    store.audit_auth_permissions(enforce=True)
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_source_record_requires_valid_reasoning_state() -> None:
    payload = {
        "source_id": "src_fixture123",
        "vendor": "anthropic",
        "protocol": "anthropic",
        "base_url": None,
        "credential_ref": "cred_fixture123",
        "allowed_origins": [],
        "model_ids": ["model-a"],
        "prefix": "avibe-fixture",
    }

    with pytest.raises(EngineStateError, match="invalid engine source reasoning state"):
        SourceRecord.from_payload(payload)

    payload["model_reasoning_efforts"] = [["other-model", ["high"]]]
    with pytest.raises(EngineStateError, match="invalid engine source reasoning state"):
        SourceRecord.from_payload(payload)


@pytest.mark.parametrize(
    ("persisted", "expected_reason"),
    [
        pytest.param(
            json.dumps(
                {
                    "sources": [
                        {
                            "source_id": "src_fixture123",
                            "vendor": "custom",
                            "protocol": "openai_chat",
                            "base_url": "https://api.example.test/v1",
                            "credential_ref": "cred_fixture123",
                            "allowed_origins": [],
                            "model_ids": ["model-a"],
                            "prefix": "avibe-fixture",
                        }
                    ]
                }
            ),
            "invalid engine source reasoning state",
            id="missing-reasoning-state",
        ),
        pytest.param(
            "{not-json",
            "invalid engine state file: sources.json",
            id="corrupt-json",
        ),
    ],
)
def test_unreadable_source_state_is_discarded_rebuilt_and_reaches_ready(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    persisted: str,
    expected_reason: str,
) -> None:
    supervisor, store = _fixture_supervisor(tmp_path)
    credential_ref = store.store_api_key(
        "upstream-secret",
        base_url="https://api.example.test/v1",
    )
    sources_path = store.root / "sources.json"
    sources_path.write_text(persisted, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="vibe.model_hub_runtime.state"):
        store.sync_sources([_binding(credential_ref)])

    warnings = [
        record
        for record in caplog.records
        if record.name == "vibe.model_hub_runtime.state"
        and record.levelno == logging.WARNING
    ]
    assert [record.getMessage() for record in warnings] == [
        f"Discarded invalid engine state file sources.json: {expected_reason}"
    ]
    assert sources_path.with_name("sources.json.invalid").read_text(encoding="utf-8") == persisted
    rebuilt = json.loads(sources_path.read_text(encoding="utf-8"))
    assert rebuilt["sources"][0]["model_reasoning_efforts"] == []

    try:
        supervisor.ensure_running()
        assert supervisor.status()["status"]["health"] == "ok"
    finally:
        supervisor.stop()


def test_valid_source_state_is_loaded_without_touching_the_file(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = EngineStateStore(tmp_path / "state")
    credential_ref = store.store_api_key(
        "upstream-secret",
        base_url="https://api.example.test/v1",
    )
    expected = store.sync_sources([_binding(credential_ref)])
    sources_path = store.root / "sources.json"
    persisted = sources_path.read_bytes()
    persisted_mtime = sources_path.stat().st_mtime_ns

    with caplog.at_level(logging.WARNING, logger="vibe.model_hub_runtime.state"):
        assert store.list_sources() == expected

    assert sources_path.read_bytes() == persisted
    assert sources_path.stat().st_mtime_ns == persisted_mtime
    assert not sources_path.with_name("sources.json.invalid").exists()
    assert not [
        record
        for record in caplog.records
        if record.name == "vibe.model_hub_runtime.state"
        and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    ("vendor", "expected_base_url"),
    [
        *[
            pytest.param(entry.id, entry.official_base_url, id=entry.id)
            for entry in api_key_vendor_catalog()
        ],
        pytest.param("codex", "https://api.openai.com/v1", id="codex"),
    ],
)
def test_api_key_vendor_catalog_populates_runtime_official_base_urls(
    vendor: str,
    expected_base_url: str,
) -> None:
    assert client_module._OFFICIAL_BASE_URLS[vendor] == expected_base_url


@pytest.mark.parametrize(
    ("vendor", "protocol", "expected_base_url"),
    API_KEY_VENDOR_RUNTIME_CASES,
)
def test_catalog_owned_api_key_sources_without_explicit_base_url_sync_and_write_engine_config(
    tmp_path: Path,
    vendor: str,
    protocol: str,
    expected_base_url: str,
) -> None:
    source_suffix = "".join(character for character in vendor.lower() if character.isalnum())
    source_id = f"src_{(source_suffix + '12345678')[:8]}"
    store = EngineStateStore(tmp_path / "state")
    instance_dir, runtime_secrets = store.prepare_instance("install-1")
    credential_ref = store.store_api_key(
        "secret",
        vendor=vendor,
        protocol=protocol,
        base_url=None,
    )
    store.sync_sources(
        [
            _binding(
                credential_ref,
                source_id=source_id,
                vendor=vendor,
                protocol=protocol,
                base_url=None,
            )
        ]
    )
    config_path = instance_dir / "config.yaml"

    write_engine_config(
        config_path,
        host="127.0.0.1",
        port=18231,
        auth_dir=store.auth_dir,
        runtime_secrets=runtime_secrets,
        sources=store.list_sources(),
        state_store=store,
    )

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if protocol == "anthropic":
        assert payload["claude-api-key"][0]["base-url"] == expected_base_url
        return
    if protocol == "openai_responses":
        assert payload["codex-api-key"][0]["base-url"] == expected_base_url
        return
    assert payload["openai-compatibility"][0]["base-url"] == expected_base_url


def test_state_removes_secret_bearing_configs_on_upgrade_and_revocation(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    old_instance, _ = store.prepare_instance("install-old")
    old_config = old_instance / "config.yaml"
    old_config.write_text("api-key: old-secret\n", encoding="utf-8")
    old_config.chmod(0o600)

    current_instance, _ = store.prepare_instance("install-current")
    assert not old_instance.exists()

    current_config = current_instance / "config.yaml"
    current_config.write_text("api-key: current-secret\n", encoding="utf-8")
    current_config.chmod(0o600)
    store.clear_runtime_configs()
    assert not current_config.exists()


def test_oauth_source_bindings_are_scoped_and_follow_reauthentication(tmp_path: Path) -> None:
    store = EngineStateStore(tmp_path / "state")
    credentials_dir = store.root / "credentials"
    credentials_dir.mkdir(parents=True, mode=0o700)
    interrupted_write = credentials_dir / ".cred_interrupted.json.temporary"
    interrupted_write.write_text("{}", encoding="utf-8")
    interrupted_write.chmod(0o600)
    first_ref = store.bind_oauth_credential(
        "src_fixture123",
        "anthropic",
        "claude-first.json",
    )
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    binding = _binding(
        first_ref,
        vendor="anthropic",
        protocol="anthropic",
        base_url=None,
        allowed_origins=("claude",),
    )

    with pytest.raises(EngineStateError, match="requires at least one allowed origin"):
        store.sync_sources([SourceBinding(**{**binding.__dict__, "allowed_origins": ()})])
    with pytest.raises(EngineStateError, match="does not match"):
        store.sync_sources([SourceBinding(**{**binding.__dict__, "source_id": "src_other1234"})])
    with pytest.raises(EngineStateError, match="does not match"):
        store.sync_sources(
            [
                SourceBinding(
                    **{
                        **binding.__dict__,
                        "vendor": "openai",
                        "base_url": "https://api.openai.com/v1",
                    }
                )
            ]
        )

    first = store.sync_sources([binding])[0]
    assert first.prefix == store.credential_metadata(first_ref)["prefix"]

    replacement_ref = store.bind_oauth_credential(
        "src_fixture123",
        "anthropic",
        "claude-replacement.json",
    )
    replacement = store.sync_sources([SourceBinding(**{**binding.__dict__, "credential_ref": replacement_ref})])[0]
    assert replacement.prefix == store.credential_metadata(replacement_ref)["prefix"]
    assert replacement.prefix != first.prefix

    assert (
        store.bind_oauth_credential(
            "src_fixture123",
            "anthropic",
            "claude-replacement.json",
        )
        == replacement_ref
    )
    with pytest.raises(EngineStateError, match="already bound to another source"):
        store.bind_oauth_credential(
            "src_other1234",
            "anthropic",
            "claude-replacement.json",
        )


@contextmanager
def _models_endpoint():
    class Handler(BaseHTTPRequestHandler):
        authorization: str | None = None

        def log_message(self, *args):
            pass

        def do_GET(self):
            Handler.authorization = self.headers.get("Authorization")
            body = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            midpoint = len(body) // 2
            self.wfile.write(body[:midpoint])
            self.wfile.flush()
            time.sleep(0.05)
            self.wfile.write(body[midpoint:])

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_model_inventory_duplicate_top_level_members_replace_earlier_values() -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            b'{"data":[{"id":"stale-data"}],'
            b'"data":[{"id":"live-data"}],'
            b'"models":[{"id":"stale-models"}],'
            b'"models":[{"id":"live-models"}]}'
        )
    )

    assert projected == (
        True,
        True,
        [DiscoveredModel(id="live-data")],
        True,
        True,
        [DiscoveredModel(id="live-models")],
    )


def test_model_inventory_duplicate_item_id_keeps_the_final_member() -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            b'{"data":[{"id":"stale","id":"live"},'
            b'{"id":"other"}]}'
        )
    )

    assert projected == (
        True,
        True,
        [DiscoveredModel(id="live"), DiscoveredModel(id="other")],
        False,
        False,
        [],
    )


@pytest.mark.parametrize(
    ("member", "result_index"),
    (("data", 2), ("models", 5)),
)
def test_model_inventory_captures_supported_parameters_from_both_shapes(
    member: str,
    result_index: int,
) -> None:
    payload = json.dumps(
        {
            member: [
                {
                    "id": "reasoning-model",
                    "supported_parameters": ["reasoning", "temperature"],
                }
            ]
        }
    ).encode()

    projected = client_module._project_model_inventory(io.BytesIO(payload))

    assert projected is not None
    assert projected[result_index] == [
        DiscoveredModel(
            id="reasoning-model",
            supported_parameters=("reasoning", "temperature"),
        )
    ]


def test_model_inventory_distinguishes_absent_and_empty_supported_parameters() -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            b'{"data":['
            b'{"id":"absent"},'
            b'{"id":"empty","supported_parameters":[]}'
            b']}'
        )
    )

    assert projected is not None
    assert projected[2] == [
        DiscoveredModel(id="absent", supported_parameters=None),
        DiscoveredModel(id="empty", supported_parameters=()),
    ]


@pytest.mark.parametrize(
    "malformed",
    (
        '"reasoning"',
        '["reasoning", 7]',
        '["reasoning", {"name":"temperature"}]',
        '["reasoning", ""]',
    ),
)
def test_model_inventory_degrades_malformed_supported_parameters_without_losing_id(
    malformed: str,
) -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            (
                '{"data":[{"id":"kept-model","supported_parameters":'
                f"{malformed}"
                "}]}"
            ).encode()
        )
    )

    assert projected is not None
    assert projected[2] == [DiscoveredModel(id="kept-model")]


def test_model_inventory_duplicate_metadata_member_replaces_its_own_scope() -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            b'{"data":['
            b'{"id":"first","supported_parameters":["stale"],'
            b'"supported_parameters":["reasoning","reasoning","temperature"]},'
            b'{"id":"second","supported_parameters":["tools"]}'
            b']}'
        )
    )

    assert projected is not None
    assert projected[2] == [
        DiscoveredModel(
            id="first",
            supported_parameters=("reasoning", "temperature"),
        ),
        DiscoveredModel(id="second", supported_parameters=("tools",)),
    ]


def test_model_inventory_duplicate_ids_keep_the_first_complete_record() -> None:
    projected = client_module._project_model_inventory(
        io.BytesIO(
            b'{"data":['
            b'{"id":"same","supported_parameters":["temperature"]},'
            b'{"id":"same","supported_parameters":["reasoning"]}'
            b']}'
        )
    )

    assert projected is not None
    assert projected[2] == [
        DiscoveredModel(id="same", supported_parameters=("temperature",))
    ]


def test_model_inventory_rejects_an_elided_model_identifier() -> None:
    oversized = b"x" * (16 * 1024 + 1)

    projected = client_module._project_model_inventory(
        io.BytesIO(b'{"data":[{"id":"valid"},{"id":"' + oversized + b'"}]}')
    )

    assert projected is None


def test_adapter_provisions_probes_and_revokes_credential(tmp_path: Path) -> None:
    class Supervisor:
        def __init__(self, store: EngineStateStore) -> None:
            self.state_store = store

        def invalidate_configs(self) -> None:
            self.state_store.clear_runtime_configs()

    async def run(base_url: str, handler) -> None:
        store = EngineStateStore(tmp_path / "state")
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store),  # type: ignore[arg-type]
            state_store=store,
        )
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_chat",
            "probe-secret",
            base_url,
        )

        models = await adapter.discover_models(
            "custom",
            "openai_chat",
            f"{base_url}/",
            credential_ref,
        )

        assert models == (
            DiscoveredModel(id="model-a"),
            DiscoveredModel(id="model-b"),
        )
        assert handler.authorization == "Bearer probe-secret"
        with pytest.raises(EngineStateError, match="does not match"):
            await adapter.discover_models(
                "custom",
                "openai_chat",
                "https://different.example/v1",
                credential_ref,
            )
        unsafe_instance = store.root / "instances" / "unsafe-entry"
        unsafe_instance.parent.mkdir(parents=True, exist_ok=True)
        unsafe_instance.write_text("not a directory", encoding="utf-8")
        with pytest.raises(EngineStateError, match="instance directory is unsafe"):
            await adapter.revoke_credential(credential_ref)
        assert store.credential_metadata(credential_ref)["value"] == "probe-secret"
        unsafe_instance.unlink()
        await adapter.revoke_credential(credential_ref)
        with pytest.raises(EngineStateError, match="unavailable"):
            store.credential_metadata(credential_ref)

    with _models_endpoint() as (base_url, handler):
        asyncio.run(run(base_url, handler))


def _write_mock_engine(
    path: Path,
    *,
    startup_delay: float = 0.0,
    startup_output: bytes = b"",
    startup_output_repeat: int = 1,
    echo_runtime_secrets: bool = False,
    exit_before_ready: int | None = None,
) -> None:
    script = f"""#!{sys.executable}
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

config_path = sys.argv[sys.argv.index('-config') + 1]
with open(config_path, encoding='utf-8') as handle:
    config = yaml.safe_load(handle)
gateway = config['api-keys'][0]
management = config['remote-management']['secret-key']
startup_output = {startup_output!r} * {startup_output_repeat!r}
if startup_output:
    sys.stdout.buffer.write(startup_output)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(startup_output)
    sys.stderr.buffer.flush()
if {echo_runtime_secrets!r}:
    sys.stderr.buffer.write(
        f'management-key={{management}} gateway-token={{gateway}}'.encode()
    )
    sys.stderr.buffer.flush()
with open('startup-output-complete', 'w', encoding='utf-8') as handle:
    handle.write(str(len(startup_output) * 2))
exit_before_ready = {exit_before_ready!r}
if exit_before_ready is not None:
    raise SystemExit(exit_before_ready)
time.sleep({startup_delay!r})
health_surfaces = set()

def mark_health_surface(surface):
    health_surfaces.add(surface)
    if len(health_surfaces) == 2:
        with open('health-surfaces-complete', 'w', encoding='utf-8') as handle:
            handle.write(','.join(sorted(health_surfaces)))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/v1/models' and self.headers.get('Authorization') == f'Bearer {{gateway}}':
            mark_health_surface('gateway')
            self._json(200, {{'object': 'list', 'data': []}})
            return
        if self.path == '/v0/management/config' and self.headers.get('X-Management-Key') == management:
            mark_health_surface('management')
            self._json(200, {{'host': config['host']}})
            return
        if self.path.startswith('/v0/management/auth-files/models'):
            self._json(200, {{'models': [{{'id': 'model-a'}}]}})
            return
        self._json(401, {{'error': {{'type': 'unauthorized'}}}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length))
        if self.headers.get('Authorization') != f'Bearer {{gateway}}':
            self._json(401, {{'error': {{'type': 'unauthorized'}}}})
            return
        if payload['model'].endswith('/rate-limited'):
            self._json(429, {{'error': {{'type': 'quota_exceeded', 'message': 'upstream-secret'}}}})
            return
        if payload['model'].endswith('/unsafe-error-code'):
            self._json(400, {{'error': {{'type': 'invalid_key_upstream-secret'}}}})
            return
        if payload['model'].endswith('/account-banned'):
            self._json(403, {{'error': {{'type': 'account_banned', 'message': 'upstream-secret'}}}})
            return
        if payload['model'].endswith('/account-suspended'):
            self._json(403, {{'error': {{'code': 'account_suspended', 'message': 'upstream-secret'}}}})
            return
        if payload['model'].endswith('/account-disabled'):
            self._json(403, {{'error': {{'type': 'vendor_error', 'code': 'account_disabled', 'message': 'upstream-secret'}}}})
            return
        if payload['model'].endswith('/ban-token-in-message'):
            self._json(403, {{'error': {{'message': 'prefix account_banned suffix upstream-secret'}}}})
            return
        if payload['model'].endswith('/redirected'):
            self.send_response(307)
            self.send_header('Location', 'https://example.test/credential-leak')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if payload['model'].endswith('/invalid-json'):
            body = b'not-json'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload['model'].endswith('/oversized-non-stream'):
            body = b'{{"payload":"' + b'x' * (17 * 1024 * 1024) + b'"}}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if payload['model'].endswith('/slow-stream'):
            first = b'data: {{"object":"chat.completion.chunk","choices":[{{"delta":{{"content":"slow"}}}}]}}\\n\\n'
            second = b'data: [DONE]\\n\\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(first) + len(second)))
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            time.sleep(0.15)
            self.wfile.write(second)
            return
        if payload.get('stream'):
            if self.path == '/v1/messages':
                body = b'event: message_stop\\ndata: {{"type":"message_stop"}}\\n\\n'
            elif self.path == '/v1/responses':
                body = b'event: response.completed\\ndata: {{"type":"response.completed"}}\\n\\n'
            else:
                body = b'data: [DONE]\\n\\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(200, {{'id': 'response-1', 'model': payload['model']}})

HTTPServer((config['host'], config['port']), Handler).serve_forever()
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


class _FixtureInstaller:
    def __init__(self, binary: Path, install_dir: Path) -> None:
        self.binary = binary
        self.install_dir = install_dir
        self.changed = True

    def ensure(self):
        result = {
            "ok": True,
            "path": str(self.binary),
            "install_dir": str(self.install_dir),
            "version": "v7.2.95",
            "changed": self.changed,
        }
        self.changed = False
        return result

    def status(self):
        return {
            "installed": True,
            "version": "v7.2.95",
            "install_dir": str(self.install_dir),
        }

    def resolve_engine_path(self):
        return self.binary

    def contract_manifest(self):
        return {
            "name": "cliproxyapi",
            "version": "v7.2.95",
            "source_sha": "f" * 40,
            "assets": [],
        }


def _fixture_supervisor(
    tmp_path: Path,
    *,
    process_factory=subprocess.Popen,
    startup_timeout: float = 5,
    startup_delay: float = 0.0,
    startup_output: bytes = b"",
    startup_output_repeat: int = 1,
    echo_runtime_secrets: bool = False,
    exit_before_ready: int | None = None,
) -> tuple[EngineSupervisor, EngineStateStore]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    binary = tmp_path / "mock-engine"
    _write_mock_engine(
        binary,
        startup_delay=startup_delay,
        startup_output=startup_output,
        startup_output_repeat=startup_output_repeat,
        echo_runtime_secrets=echo_runtime_secrets,
        exit_before_ready=exit_before_ready,
    )
    installer = _FixtureInstaller(binary, tmp_path / "versions" / "install-1")
    store = EngineStateStore(tmp_path / "state")
    return (
        EngineSupervisor(
            installer=installer,
            state_store=store,
            startup_timeout=startup_timeout,
            process_factory=process_factory,
        ),
        store,
    )


@pytest.mark.parametrize(
    ("installed", "start_attempted", "running", "healthy", "expected"),
    [
        (False, False, False, False, "not_installed"),
        (False, True, False, False, "not_installed"),
        (True, False, False, False, "not_started"),
        (True, True, False, False, "down"),
        (True, True, True, False, "degraded"),
        (True, True, True, True, "ok"),
    ],
)
def test_supervisor_status_distinguishes_all_runtime_health_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
    start_attempted: bool,
    running: bool,
    healthy: bool,
    expected: str,
) -> None:
    installer = SimpleNamespace(
        status=lambda: {"installed": installed, "version": "v7.2.95" if installed else None},
        contract_manifest=lambda: {"name": "cliproxyapi", "version": "v7.2.95", "assets": []},
    )
    supervisor = EngineSupervisor(
        installer=installer,
        state_store=EngineStateStore(tmp_path / expected),
    )
    supervisor._start_attempted = start_attempted
    if running:
        supervisor._process = SimpleNamespace(poll=lambda: None)
        supervisor._connection = EngineConnection("http://127.0.0.1:15220", "management", "gateway")
    monkeypatch.setattr(supervisor, "_healthy_locked", lambda: healthy)

    assert supervisor.status()["status"]["health"] == expected


def test_supervisor_missing_runtime_stays_installable_after_start(tmp_path: Path) -> None:
    installer = SimpleNamespace(
        resolve_engine_path=lambda: None,
        status=lambda: {
            "installed": False,
            "version": None,
            "reason": "fixture_install_failed",
        },
        contract_manifest=lambda: {"name": "cliproxyapi", "version": "v7.2.95", "assets": []},
    )
    supervisor = EngineSupervisor(
        installer=installer,
        state_store=EngineStateStore(tmp_path / "failed-install"),
    )

    with pytest.raises(EngineUnavailableError, match="models.engine.install_failed"):
        supervisor.ensure_running()

    assert supervisor.status()["status"]["health"] == "not_installed"


def test_supervisor_starts_disk_engine_despite_released_failure_state_and_missing_manifest(
    tmp_path: Path,
) -> None:
    source_binary = tmp_path / "fixture" / "mock-engine"
    source_binary.parent.mkdir(parents=True)
    _write_mock_engine(source_binary)
    binary = source_binary.read_text(encoding="utf-8").replace(
        "\nimport json\n",
        "\nimport sys\n"
        "if sys.argv[1:] == ['--help']:\n"
        "    print('CLIProxyAPI Version: 7.2.95, Commit: fixture')\n"
        "    raise SystemExit(0)\n"
        "import json\n",
        1,
    ).encode()
    archive = tmp_path / "fixture" / "CLIProxyAPI_fixture.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("cli-proxy-api")
        member.mode = 0o755
        member.size = len(binary)
        tar.addfile(member, io.BytesIO(binary))
    manifest_path = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
    manager = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest_path)
    installed = manager.ensure()
    assert installed["ok"] is True
    assert manager.transition_install_claim(
        InstallClaimTransition.CREATE,
        generation=RUNTIME_INSTALL_GENERATION_A,
        target=installed["target"],
    )
    assert manager.transition_install_claim(
        InstallClaimTransition.SETTLE_FAILURE,
        generation=RUNTIME_INSTALL_GENERATION_A,
        reason="model_hub_engine_manifest_missing",
    )
    manifest_path.unlink()
    manager.offline = True
    supervisor = EngineSupervisor(
        installer=manager,
        state_store=EngineStateStore(tmp_path / "state"),
        startup_timeout=5,
    )

    assert manager.status()["installed"] is True
    assert supervisor.status()["status"]["health"] == "not_started"
    connection = supervisor.ensure_running()
    assert connection.base_url.startswith("http://127.0.0.1:")
    assert supervisor.status()["status"]["health"] == "ok"
    supervisor.stop()


def test_supervisor_keeps_installing_state_unverified_until_settlement(
    tmp_path: Path,
) -> None:
    installer = SimpleNamespace(
        status=lambda: {
            "installed": True,
            "version": "v7.2.95",
            "platform": "darwin-arm64",
        },
        install_state=lambda: {"state": "installing", "error_key": None},
        host_platform=lambda: "darwin-arm64",
        contract_manifest=lambda: {
            "name": "cliproxyapi",
            "version": "v7.2.95",
            "assets": [],
        },
    )
    supervisor = EngineSupervisor(
        installer=installer,
        state_store=EngineStateStore(tmp_path / "state"),
    )

    status = supervisor.status()["status"]

    assert status["health"] == "installing"
    assert status["installed_version"] is None
    assert status["verified"] is False


def test_supervisor_starts_checks_health_and_stops_mock_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}
    captured_stdio: dict[str, int] = {}

    def spawn(*args, **kwargs):
        captured_env.update(kwargs["env"])
        captured_stdio.update(stdout=kwargs["stdout"], stderr=kwargs["stderr"])
        return subprocess.Popen(*args, **kwargs)

    monkeypatch.setenv("MANAGEMENT_PASSWORD", "untrusted-management-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "unrelated-github-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    supervisor, store = _fixture_supervisor(tmp_path, process_factory=spawn)

    assert supervisor.status()["status"]["health"] == "not_started"
    first = supervisor.ensure_running()
    assert first.base_url.startswith("http://127.0.0.1:")
    assert "MANAGEMENT_PASSWORD" not in captured_env
    assert "OPENAI_API_KEY" not in captured_env
    assert "GITHUB_TOKEN" not in captured_env
    assert "HTTP_PROXY" not in captured_env
    assert captured_env == engine_subprocess_environment()
    assert captured_stdio == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    assert supervisor.status()["status"]["health"] == "ok"
    config_path = store.root / "instances" / "install-1" / "config.yaml"
    first_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert first_config["api-keys"] == [first.gateway_token]
    assert first_config["remote-management"]["secret-key"] == first.management_key

    supervisor.stop()
    assert supervisor.status()["status"]["health"] == "down"
    second = supervisor.ensure_running()
    assert second.gateway_token == first.gateway_token
    assert second.management_key == first.management_key
    supervisor.stop()


def test_supervisor_disable_restores_explicit_off_state(tmp_path: Path) -> None:
    supervisor, _store = _fixture_supervisor(tmp_path)
    supervisor.ensure_running()

    supervisor.disable()

    assert supervisor.status()["status"]["health"] == "not_started"
    supervisor.ensure_running()
    assert supervisor.status()["status"]["health"] == "ok"
    supervisor.stop()


def _assert_supervisor_owned_startup_log(
    message: str,
    *,
    outcome: str,
    exit_code: int | None = None,
    readiness_budget: float | None = None,
) -> None:
    prefix = "Model Hub engine startup "
    assert message.startswith(prefix)
    tokens = message.removeprefix(prefix).split()
    assert all("=" in token for token in tokens)
    fields = dict(token.split("=", 1) for token in tokens)
    assert len(fields) == len(tokens)

    expected_fields = {
        "outcome",
        "managed_version",
        "elapsed_seconds",
        "child_output_retained",
    }
    if readiness_budget is not None:
        expected_fields.update({"exit_code", "readiness_budget_seconds"})
    assert set(fields) == expected_fields
    assert fields["outcome"] == outcome
    assert fields["managed_version"] == "v7.2.95"
    assert float(fields["elapsed_seconds"]) >= 0
    assert fields["child_output_retained"] == "false"
    if readiness_budget is not None:
        assert fields["exit_code"] == str(exit_code)
        assert float(fields["readiness_budget_seconds"]) == readiness_budget
    assert len(message.encode("utf-8")) < 512


def _assert_child_payload_absent(log_text: str, payload: bytes) -> None:
    printable_run = bytearray()
    candidates: list[bytes] = []
    for byte in payload + b"\x00":
        if 32 <= byte <= 126:
            printable_run.append(byte)
            continue
        if len(printable_run) >= 8:
            if len(printable_run) <= 128:
                candidates.append(bytes(printable_run))
            else:
                candidates.extend((bytes(printable_run[:64]), bytes(printable_run[-64:])))
        printable_run.clear()

    assert candidates
    assert all(candidate.decode("ascii") not in log_text for candidate in candidates)
    assert "\ufffd" not in log_text


def test_mh_runtime_005_first_cold_start_waits_for_readiness_within_bounded_budget(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MH-RUNTIME-005: first cold start waits past an early probe and becomes ready."""

    assert MODEL_HUB_STARTUP_TIMEOUT_SECONDS == 30.0
    caplog.set_level(logging.INFO, logger="vibe.model_hub_runtime.supervisor")
    child_payload = b"\n".join(
        (
            b"cold-start-safe-diagnostic-marker",
            b'id_token="opaque-id-token-value"',
            b'private_key="opaque-private-key-value"',
            b'future_unknown_auth_material="opaque-unknown-value"',
            b"binary-boundary-before-\xff\xfe-binary-boundary-after",
        )
    )
    supervisor, store = _fixture_supervisor(
        tmp_path,
        startup_timeout=3.0,
        startup_delay=0.25,
        startup_output=child_payload,
        echo_runtime_secrets=True,
    )

    started_at = time.monotonic()
    connection = supervisor.ensure_running()
    elapsed = time.monotonic() - started_at
    runtime_secrets = store.prepare_instance("install-1", rotate=False)[1]
    instance_dir = store.root / "instances" / "install-1"
    ready_log = next(
        record.getMessage()
        for record in caplog.records
        if "startup outcome=ready" in record.getMessage()
    )

    assert connection.base_url.startswith("http://127.0.0.1:")
    assert 0.2 <= elapsed < 3.0
    _assert_supervisor_owned_startup_log(ready_log, outcome="ready")
    _assert_child_payload_absent(caplog.text, child_payload)
    for secret in (runtime_secrets.management_key, runtime_secrets.gateway_token):
        midpoint = len(secret) // 2
        assert secret[:midpoint] not in caplog.text
        assert secret[midpoint:] not in caplog.text
    assert (instance_dir / "startup-output-complete").read_text(encoding="utf-8") == str(
        len(child_payload) * 2
    )
    assert (instance_dir / "health-surfaces-complete").read_text(encoding="utf-8") == (
        "gateway,management"
    )
    supervisor.stop()


def test_supervisor_process_exit_discards_all_child_output(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    child_payload = b"\n".join(
        (
            b"process-exit-safe-diagnostic-marker",
            b'id_token="process-exit-oauth-secret"',
            b'private_key="process-exit-private-key"',
            b'unknown_field="process-exit-opaque-value"',
            b"process-exit-binary-before-\xff-process-exit-binary-after",
        )
    )
    caplog.set_level(logging.WARNING, logger="vibe.model_hub_runtime.supervisor")
    supervisor, store = _fixture_supervisor(
        tmp_path,
        startup_timeout=2.0,
        startup_output=child_payload,
        echo_runtime_secrets=True,
        exit_before_ready=23,
    )

    with pytest.raises(EngineUnavailableError, match="models.engine.health_failed") as exc_info:
        supervisor.ensure_running()

    runtime_secrets = store.prepare_instance("install-1", rotate=False)[1]
    instance_dir = store.root / "instances" / "install-1"
    warning = next(
        record.getMessage()
        for record in caplog.records
        if "startup outcome=process_exit" in record.getMessage()
    )
    assert exc_info.value.error_key == "models.engine.health_failed"
    assert supervisor.status()["status"]["health"] == "down"
    _assert_supervisor_owned_startup_log(
        warning,
        outcome="process_exit",
        exit_code=23,
        readiness_budget=2.0,
    )
    _assert_child_payload_absent(caplog.text, child_payload)
    for secret in (runtime_secrets.management_key, runtime_secrets.gateway_token):
        midpoint = len(secret) // 2
        assert secret[:midpoint] not in caplog.text
        assert secret[midpoint:] not in caplog.text
    assert (instance_dir / "startup-output-complete").read_text(encoding="utf-8") == str(
        len(child_payload) * 2
    )
    assert not (instance_dir / "health-surfaces-complete").exists()


def test_mh_runtime_006_timeout_stops_engine_with_bounded_structured_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MH-RUNTIME-006: readiness timeout is terminal with bounded diagnostics."""

    caplog.set_level(logging.WARNING, logger="vibe.model_hub_runtime.supervisor")
    child_payload = b"\n".join(
        (
            b"timeout-safe-diagnostic-marker",
            b'id_token="timeout-oauth-secret"',
            b'private_key="timeout-private-key"',
            b'future_unknown_field="timeout-opaque-value"',
            b"oversized-child-output-" + b"x" * 4_096,
            b"timeout-binary-before-\xff\xfe-timeout-binary-after",
        )
    )
    output_repeat = 256
    supervisor, store = _fixture_supervisor(
        tmp_path,
        startup_timeout=1.0,
        startup_delay=60.0,
        startup_output=child_payload,
        startup_output_repeat=output_repeat,
        echo_runtime_secrets=True,
    )

    started_at = time.monotonic()
    with pytest.raises(EngineUnavailableError, match="models.engine.health_failed") as exc_info:
        supervisor.ensure_running()
    elapsed = time.monotonic() - started_at
    runtime_secrets = store.prepare_instance("install-1", rotate=False)[1]
    instance_dir = store.root / "instances" / "install-1"
    warning = next(
        record.getMessage()
        for record in caplog.records
        if "startup outcome=timeout" in record.getMessage()
    )

    assert exc_info.value.error_key == "models.engine.health_failed"
    assert elapsed < 2.5
    assert supervisor.status()["status"]["health"] == "down"
    _assert_supervisor_owned_startup_log(
        warning,
        outcome="timeout",
        readiness_budget=1.0,
    )
    _assert_child_payload_absent(caplog.text, child_payload)
    for secret in (runtime_secrets.management_key, runtime_secrets.gateway_token):
        midpoint = len(secret) // 2
        assert secret[:midpoint] not in caplog.text
        assert secret[midpoint:] not in caplog.text
    observed_output_bytes = int(
        (instance_dir / "startup-output-complete").read_text(encoding="utf-8")
    )
    assert observed_output_bytes == len(child_payload) * output_repeat * 2
    assert observed_output_bytes > 512 * 1024


def test_mh_runtime_001_service_restart_reports_installed_engine_as_not_started(
    tmp_path: Path,
) -> None:
    """MH-RUNTIME-001: a service restart restores lazy-start idleness, never down."""

    before_restart, store = _fixture_supervisor(tmp_path)
    before_restart.ensure_running()
    observed_health = [before_restart.status()["status"]["health"]]
    before_restart.stop()

    after_restart = EngineSupervisor(
        installer=before_restart.installer,
        state_store=store,
        startup_timeout=5,
    )
    observed_health.append(after_restart.status()["status"]["health"])

    assert observed_health == ["ok", "not_started"]
    assert "down" not in observed_health


def test_adapter_enforces_origin_and_returns_raw_outcomes(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_chat",
            "upstream-secret",
            "https://api.example.test/v1",
        )
        await adapter.sync_sources(
            [
                _binding(
                    credential_ref,
                    allowed_origins=("codex",),
                    model_ids=(
                        "model-a",
                        "rate-limited",
                        "unsafe-error-code",
                        "account-banned",
                        "account-suspended",
                        "account-disabled",
                        "ban-token-in-message",
                        "redirected",
                    ),
                )
            ]
        )

        started = await adapter.start()
        assert started.health is EngineHealth.OK
        assert started.listen_host == "127.0.0.1"
        assert await adapter.gateway_token()

        with pytest.raises(OriginNotAllowedError):
            await adapter.invoke("src_fixture123", "model-a", {}, False, "claude")

        handle = await adapter.invoke("src_fixture123", "model-a", {}, False, "codex")
        assert handle.stream is not None
        payload = b"".join([chunk async for chunk in handle.stream])
        assert json.loads(payload)["model"].endswith("/model-a")
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is True

        failed = await adapter.invoke("src_fixture123", "rate-limited", {}, False, "codex")
        assert failed.stream is None
        failure = await failed.outcome()
        assert failure.kind is RawOutcomeKind.HTTP_ERROR
        assert failure.http_status == 429
        assert failure.error_type == "quota_exceeded"
        assert "upstream-secret" not in (failure.redacted_message or "")
        unsafe_code = await adapter.invoke("src_fixture123", "unsafe-error-code", {}, False, "codex")
        assert (await unsafe_code.outcome()).error_code is None
        for model_id, error_code in (
            ("account-banned", "account_banned"),
            ("account-suspended", "account_suspended"),
            ("account-disabled", "account_disabled"),
        ):
            banned = await adapter.invoke("src_fixture123", model_id, {}, False, "codex")
            banned_outcome = await banned.outcome()
            assert error_code in banned_outcome.error_candidates
            assert banned_outcome.redacted_message == "upstream returned HTTP 403"
            assert classify_outcome(banned_outcome).reason == "account_banned"
        free_text = await adapter.invoke(
            "src_fixture123",
            "ban-token-in-message",
            {},
            False,
            "codex",
        )
        free_text_outcome = await free_text.outcome()
        assert free_text_outcome.error_code is None
        assert "account_banned" not in (free_text_outcome.redacted_message or "")
        assert "upstream-secret" not in (free_text_outcome.redacted_message or "")
        assert classify_outcome(free_text_outcome).reason == "credential_revoked"
        redirected = await adapter.invoke("src_fixture123", "redirected", {}, False, "codex")
        redirect_outcome = await redirected.outcome()
        assert redirect_outcome.kind is RawOutcomeKind.HTTP_ERROR
        assert redirect_outcome.http_status == 307
        await adapter.stop()
        with pytest.raises(EngineStateError, match="still bound"):
            await adapter.revoke_credential(credential_ref)
        await adapter.sync_sources([])
        await adapter.revoke_credential(credential_ref)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("source_protocol", "origin", "caller_protocol", "request_protocol"),
    [
        ("openai_chat", "claude", "openai_chat", "anthropic"),
        ("anthropic", "codex", "anthropic", "openai_responses"),
        ("openai_chat", "opencode", "anthropic", "anthropic"),
    ],
)
def test_adapter_uses_origin_protocol_for_engine_translation(
    tmp_path: Path,
    source_protocol: str,
    origin: str,
    caller_protocol: str,
    request_protocol: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.request_protocol = None

        async def invoke(
            self,
            source,
            model_id,
            request,
            *,
            stream,
            request_protocol=None,
            request_headers=None,
        ):
            self.request_protocol = request_protocol
            self.request_headers = request_headers
            return object()

    class Supervisor:
        def __init__(self, client: Client) -> None:
            self._client = client

        def client(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        credential_ref = store.store_api_key(
            "upstream-secret",
            vendor="custom",
            protocol=source_protocol,
            base_url="https://api.example.test/v1",
        )
        store.sync_sources(
            [
                _binding(
                    credential_ref,
                    protocol=source_protocol,
                    allowed_origins=(origin,),
                )
            ]
        )
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(client),  # type: ignore[arg-type]
            state_store=store,
        )

        request = ModelHubRequest(
            {},
            protocol=caller_protocol,
            headers={"anthropic-beta": "interleaved-thinking", "authorization": "never-forward"},
        )
        await adapter.invoke("src_fixture123", "model-a", request, False, origin)

        assert client.request_protocol == request_protocol
        assert client.request_headers == {
            "anthropic-beta": "interleaved-thinking",
            "authorization": "never-forward",
        }

    asyncio.run(run())


def test_adapter_restores_source_projection_when_restart_fails(tmp_path: Path) -> None:
    class Supervisor:
        def __init__(self) -> None:
            self.restore_calls = 0

        def client_if_running(self):
            return object()

        def restart_if_running(self) -> None:
            raise EngineUnavailableError("models.engine.health_failed")

        def ensure_running(self):
            self.restore_calls += 1
            return object()

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        old_ref = store.store_api_key(
            "old-secret",
            base_url="https://old.example.test/v1",
        )
        new_ref = store.store_api_key(
            "new-secret",
            base_url="https://new.example.test/v1",
        )
        old_binding = _binding(old_ref, base_url="https://old.example.test/v1")
        store.sync_sources([old_binding])
        supervisor = Supervisor()
        adapter = CLIProxyEngineAdapter(
            supervisor=supervisor,  # type: ignore[arg-type]
            state_store=store,
        )

        with pytest.raises(EngineUnavailableError, match="models.engine.health_failed"):
            await adapter.sync_sources([_binding(new_ref, base_url="https://new.example.test/v1")])

        restored = store.get_source("src_fixture123")
        assert restored is not None
        assert restored.credential_ref == old_ref
        assert supervisor.restore_calls == 1

    asyncio.run(run())


def test_adapter_serializes_source_sync_with_new_invocations(tmp_path: Path) -> None:
    restart_started = threading.Event()
    allow_restart = threading.Event()
    invoked_refs: list[str] = []

    class Client:
        async def invoke(
            self,
            source,
            model_id,
            request,
            *,
            stream,
            request_protocol=None,
            request_headers=None,
        ):
            invoked_refs.append(source.credential_ref)
            return object()

    class Supervisor:
        def __init__(self) -> None:
            self._client = Client()

        def client_if_running(self):
            return self._client

        def restart_if_running(self) -> None:
            restart_started.set()
            assert allow_restart.wait(timeout=2)

        def client(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        old_ref = store.store_api_key(
            "old-secret",
            base_url="https://old.example.test/v1",
        )
        new_ref = store.store_api_key(
            "new-secret",
            base_url="https://new.example.test/v1",
        )
        store.sync_sources([_binding(old_ref, base_url="https://old.example.test/v1")])
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(),  # type: ignore[arg-type]
            state_store=store,
        )

        sync_task = asyncio.create_task(
            adapter.sync_sources([_binding(new_ref, base_url="https://new.example.test/v1")])
        )
        assert await asyncio.to_thread(restart_started.wait, 2)
        invoke_task = asyncio.create_task(adapter.invoke("src_fixture123", "model-a", {}, False, "codex"))
        await asyncio.sleep(0.05)
        assert not invoke_task.done()

        allow_restart.set()
        await sync_task
        await invoke_task
        assert invoked_refs == [new_ref]

    asyncio.run(run())


def test_adapter_engine_unavailable_does_not_forge_an_upstream_error_code(tmp_path: Path) -> None:
    class Supervisor:
        def client(self):
            raise EngineUnavailableError("models.engine.health_failed")

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        credential_ref = store.store_api_key(
            "upstream-secret",
            base_url="https://api.example.test/v1",
        )
        store.sync_sources([_binding(credential_ref)])
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(),  # type: ignore[arg-type]
            state_store=store,
        )

        handle = await adapter.invoke("src_fixture123", "model-a", {}, False, "codex")
        outcome = await handle.outcome()

        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.error_code == "engine_down"
        assert outcome.redacted_message is None

    asyncio.run(run())


@pytest.mark.parametrize(("changed", "expected_restarts"), [(False, 0), (True, 1)])
def test_adapter_applies_changed_install_to_running_engine(
    tmp_path: Path,
    changed: bool,
    expected_restarts: int,
) -> None:
    class Installer:
        def ensure(self):
            return {"ok": True, "changed": changed}

    class Supervisor:
        def __init__(self) -> None:
            self.installer = Installer()
            self.restarts = 0

        def restart_if_running(self) -> None:
            self.restarts += 1

        def status(self):
            return {
                "status": {
                    "health": "down",
                    "installed_version": "v7.2.95",
                    "verified": True,
                    "listening": None,
                    "last_check": None,
                }
            }

    async def run() -> None:
        supervisor = Supervisor()
        adapter = CLIProxyEngineAdapter(
            supervisor=supervisor,  # type: ignore[arg-type]
            state_store=EngineStateStore(tmp_path / "state"),
        )

        status = await adapter.ensure_installed()

        assert status.installed_version == "v7.2.95"
        assert status.verified is True
        assert supervisor.restarts == expected_restarts

    asyncio.run(run())


def test_runtime_install_state_survives_adapter_reload_and_settles_once(
    tmp_path: Path,
) -> None:
    class BlockingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.release = threading.Event()
            self.started = threading.Event()
            self.ensure_calls = 0
            self.binary = runtime_dir / "installed-engine"

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            assert expected_target is None
            self.ensure_calls += 1
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.started.set()
            assert self.release.wait(timeout=2)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

    async def run() -> None:
        runtime_dir = tmp_path / "runtime"
        installer = BlockingInstaller(runtime_dir)
        supervisor = EngineSupervisor(
            installer=installer,
            state_store=EngineStateStore(tmp_path / "state"),
        )
        adapter = CLIProxyEngineAdapter(supervisor=supervisor)

        started = await adapter.install()
        await asyncio.to_thread(installer.started.wait, 2)
        repeated = await adapter.install()

        reloaded_installer = BlockingInstaller(runtime_dir)
        reloaded = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=reloaded_installer,
                state_store=EngineStateStore(tmp_path / "reloaded-state"),
            )
        )
        reloaded_status = await reloaded.status()

        assert started.health is EngineHealth.INSTALLING
        assert repeated.health is EngineHealth.INSTALLING
        assert reloaded_status.health is EngineHealth.INSTALLING
        assert installer.ensure_calls == 1

        installer.release.set()
        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.NOT_STARTED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("installation did not settle")

        assert settled.verified is True
        assert settled.error_key is None
        assert reloaded_installer.install_state() is None

    asyncio.run(run())


def test_cancelled_install_admission_keeps_owned_worker_and_shutdown_joins_it(
    tmp_path: Path,
) -> None:
    class BlockingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.claim_entered = threading.Event()
            self.release_claim = threading.Event()
            self.worker_started = threading.Event()
            self.release_worker = threading.Event()
            self.ensure_calls = 0
            self.binary = runtime_dir / "installed-engine"

        def transition_install_claim(self, transition, **kwargs):
            if transition is InstallClaimTransition.CREATE:
                self.claim_entered.set()
                assert self.release_claim.wait(timeout=2)
            return super().transition_install_claim(transition, **kwargs)

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            assert expected_target is None
            self.ensure_calls += 1
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.worker_started.set()
            assert self.release_worker.wait(timeout=2)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

    async def run() -> None:
        installer = BlockingInstaller(tmp_path / "runtime")
        supervisor = EngineSupervisor(
            installer=installer,
            state_store=EngineStateStore(tmp_path / "state"),
        )
        supervisor._start_attempted = True
        adapter = CLIProxyEngineAdapter(supervisor=supervisor)

        request = asyncio.create_task(adapter.install())
        assert await asyncio.to_thread(installer.claim_entered.wait, 2)
        request.cancel()
        installer.release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await request

        assert await asyncio.to_thread(installer.worker_started.wait, 2)
        repeated = await adapter.install()
        assert repeated.health is EngineHealth.INSTALLING
        assert installer.ensure_calls == 1

        stopping = asyncio.create_task(adapter.stop())
        await asyncio.sleep(0)
        assert stopping.done() is False
        installer.release_worker.set()
        await stopping

        assert installer.install_state() is None
        assert installer.resolve_engine_path() is not None
        assert (await adapter.status()).health is EngineHealth.NOT_STARTED

    asyncio.run(run())


def test_install_finalization_never_projects_a_verified_installing_state(
    tmp_path: Path,
) -> None:
    class FinalizingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.clear_entered = threading.Event()
            self.release_clear = threading.Event()
            self.binary = runtime_dir / "installed-engine"

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            assert expected_target is None
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

        def transition_install_claim(self, transition, **kwargs):
            if transition is InstallClaimTransition.SETTLE_SUCCESS:
                self.clear_entered.set()
                assert self.release_clear.wait(timeout=2)
            return super().transition_install_claim(transition, **kwargs)

    async def run() -> None:
        installer = FinalizingInstaller(tmp_path / "runtime")
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        started = await adapter.install()
        assert started.health is EngineHealth.INSTALLING
        assert await asyncio.to_thread(installer.clear_entered.wait, 2)

        finalizing = await adapter.status()
        assert finalizing.health is EngineHealth.INSTALLING
        assert finalizing.installed_version is None
        assert finalizing.verified is False

        installer.release_clear.set()
        await adapter.stop()
        settled = await adapter.status()
        assert settled.health is EngineHealth.NOT_STARTED
        assert settled.verified is True

    asyncio.run(run())


def test_orphaned_install_state_is_reclaimed_before_runtime_status(
    tmp_path: Path,
) -> None:
    class RecoveringInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.ensure_calls = 0
            self.expected_target = None
            self.binary = runtime_dir / "installed-engine"

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            self.ensure_calls += 1
            self.expected_target = expected_target
            assert expected_target == RUNTIME_INSTALL_TARGET
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

    async def run() -> None:
        installer = RecoveringInstaller(tmp_path / "runtime")
        _create_runtime_install_claim(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        recovering = await adapter.recover_installation()

        assert recovering.health in {
            EngineHealth.INSTALLING,
            EngineHealth.NOT_STARTED,
        }
        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.NOT_STARTED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("orphaned installation did not recover")

        assert installer.ensure_calls == 1
        assert installer.expected_target == RUNTIME_INSTALL_TARGET
        assert installer.install_state() is None

    asyncio.run(run())


def test_recovery_retries_a_transient_shared_install_lock_collision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class RetryingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.ensure_calls = 0
            self.first_collision = threading.Event()
            self.second_attempt = threading.Event()
            self.release = threading.Event()
            self.binary = runtime_dir / "installed-engine"

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            self.ensure_calls += 1
            assert expected_target == RUNTIME_INSTALL_TARGET
            if self.ensure_calls == 1:
                self.first_collision.set()
                return {
                    "ok": False,
                    "changed": False,
                    "reason": "model_hub_engine_install_already_running",
                    "skipped": True,
                }
            self.second_attempt.set()
            assert self.release.wait(timeout=2)
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

    async def run() -> None:
        monkeypatch.setattr(
            runtime_adapter_module,
            "_INSTALL_RECOVERY_INITIAL_DELAY_SECONDS",
            0,
        )
        monkeypatch.setattr(
            runtime_adapter_module,
            "_INSTALL_RECOVERY_MAX_DELAY_SECONDS",
            0,
        )
        installer = RetryingInstaller(tmp_path / "runtime")
        _create_runtime_install_claim(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        recovered = await adapter.recover_installation()

        assert recovered.health is EngineHealth.INSTALLING
        assert await asyncio.to_thread(installer.first_collision.wait, 2)
        assert await asyncio.to_thread(installer.second_attempt.wait, 2)
        assert (await adapter.status()).health is EngineHealth.INSTALLING
        installer.release.set()
        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.NOT_STARTED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("installation recovery did not retry")

        assert installer.ensure_calls == 2
        assert installer.install_state() is None

    asyncio.run(run())


def test_recovery_lock_wait_exhaustion_settles_terminal_with_backoff(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class CollidingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.ensure_calls = 0

        def ensure(self, **_kwargs):
            self.ensure_calls += 1
            return {
                "ok": False,
                "changed": False,
                "reason": "model_hub_engine_install_already_running",
                "skipped": True,
            }

    async def run() -> None:
        monkeypatch.setattr(runtime_adapter_module, "_INSTALL_RECOVERY_WAIT_SECONDS", 0.02)
        monkeypatch.setattr(
            runtime_adapter_module,
            "_INSTALL_RECOVERY_INITIAL_DELAY_SECONDS",
            0.001,
        )
        monkeypatch.setattr(
            runtime_adapter_module,
            "_INSTALL_RECOVERY_MAX_DELAY_SECONDS",
            0.004,
        )
        installer = CollidingInstaller(tmp_path / "runtime")
        _create_runtime_install_claim(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        recovered = await adapter.recover_installation()
        assert recovered.health is EngineHealth.INSTALLING
        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.NOT_INSTALLED:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("bounded recovery did not settle")

        state = installer.install_state()
        assert installer.ensure_calls > 2
        assert adapter._install_owner_active is False
        assert settled.error_key == "settings.models.install.fail.detail"
        assert state is not None
        assert state["state"] == "not_installed"
        assert state["reason"] == "model_hub_engine_install_lock_timeout"

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert sum("waiting up to" in record.message for record in caplog.records) == 1
    assert sum("gave up waiting" in record.message for record in caplog.records) == 1


def test_recovery_schedule_failure_abandons_the_owned_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run() -> None:
        installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
        _create_runtime_install_claim(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        def fail_schedule(**_kwargs) -> None:
            raise RuntimeError("fixture schedule failure")

        monkeypatch.setattr(adapter, "_start_install_task_locked", fail_schedule)

        recovered = await adapter.recover_installation()

        state = installer.install_state()
        assert recovered.health is EngineHealth.NOT_INSTALLED
        assert recovered.error_key == "settings.models.install.fail.detail"
        assert adapter._install_owner_active is False
        assert state is not None
        assert state["state"] == "not_installed"
        assert state["reason"] == "model_hub_engine_install_schedule_failed"

    asyncio.run(run())


def test_platform_refusal_never_creates_or_settles_an_install_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def run() -> None:
        archive, binary = _write_fixture_archive(tmp_path / "fixture")
        manifest_path = _write_fixture_manifest(tmp_path / "fixture", archive, binary)
        monkeypatch.setattr(managed_runtime, "runtime_platform_tag", lambda: "win32-x64")
        installer = EngineRuntimeManager(
            runtime_dir=tmp_path / "runtime",
            manifest_path=manifest_path,
            offline=False,
        )
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        with pytest.raises(RuntimePlatformUnsupportedError):
            await adapter.install()

        assert installer.install_state() is None
        projected = await adapter.status()
        assert projected.health is EngineHealth.NOT_INSTALLED
        assert projected.error_key is None
        assert (
            adapter.supervisor.status()["manifest"]["resolution"]
            == ManifestResolution.UNSUPPORTED.value
        )

    asyncio.run(run())


def test_every_pre_resolution_failure_persists_unless_platform_is_unsupported(
    tmp_path: Path,
) -> None:
    class FailedBeforeResolutionInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path, reason: str) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.failure_reason = reason

        def ensure(self, **_kwargs):
            return {
                "ok": False,
                "changed": False,
                "reason": self.failure_reason,
            }

    async def run() -> None:
        vocabulary = EngineRuntimeManager(
            runtime_dir=tmp_path / "vocabulary",
            offline=True,
        ).install_failure_reasons()
        for index, reason in enumerate(sorted(vocabulary)):
            installer = FailedBeforeResolutionInstaller(
                tmp_path / f"runtime-{index}",
                reason,
            )
            adapter = CLIProxyEngineAdapter(
                supervisor=EngineSupervisor(
                    installer=installer,
                    state_store=EngineStateStore(tmp_path / f"state-{index}"),
                )
            )

            with pytest.raises((EngineUnavailableError, RuntimePlatformUnsupportedError)):
                await adapter.install()

            state = EngineRuntimeManager(
                runtime_dir=installer.runtime_dir,
                offline=True,
            ).install_state()
            projected = await adapter.status()
            if reason == "model_hub_engine_platform_unsupported":
                assert state is None
                assert projected.error_key is None
            else:
                assert state is not None
                assert state["state"] == "not_installed"
                assert state["reason"] == reason
                assert projected.error_key == "settings.models.install.fail.detail"

    asyncio.run(run())


def test_runtime_start_consults_install_owner_before_starting(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
        _create_runtime_install_claim(installer)
        supervisor = EngineSupervisor(
            installer=installer,
            state_store=EngineStateStore(tmp_path / "state"),
        )
        start_calls = 0

        def fail_start() -> None:
            nonlocal start_calls
            start_calls += 1
            raise AssertionError("installing runtime started")

        supervisor.ensure_running = fail_start  # type: ignore[method-assign]
        adapter = CLIProxyEngineAdapter(supervisor=supervisor)

        status = await adapter.start()

        assert status.health is EngineHealth.INSTALLING
        assert status.listen_port is None
        assert start_calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("stop_wins", [False, True])
def test_runtime_start_after_install_obeys_latest_explicit_lifecycle_action(
    tmp_path: Path,
    stop_wins: bool,
) -> None:
    class BlockingInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.release = threading.Event()
            self.started = threading.Event()
            self.binary = runtime_dir / "installed-engine"

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            assert expected_target is None
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            self.started.set()
            assert self.release.wait(timeout=2)
            self.binary.parent.mkdir(parents=True, exist_ok=True)
            self.binary.write_bytes(b"verified fixture")
            return {
                "ok": True,
                "changed": True,
                "path": str(self.binary),
                "install_dir": str(self.binary.parent),
                "version": "v7.2.95",
            }

        def resolve_engine_path(self):
            return self.binary if self.binary.is_file() else None

        def status(self):
            installed = self.resolve_engine_path() is not None
            return {
                "installed": installed,
                "version": "v7.2.95" if installed else None,
                "install_dir": str(self.binary.parent),
                "platform": self.host_platform(),
                "reason": None,
            }

    class Supervisor:
        def __init__(self, installer: BlockingInstaller) -> None:
            self.installer = installer
            self.state_store = EngineStateStore(tmp_path / "state")
            self.start_calls = 0
            self.disable_calls = 0

        def status(self):
            installed = self.installer.resolve_engine_path() is not None
            return {
                "host_platform": self.installer.host_platform(),
                "status": {
                    "health": "ok" if self.start_calls else (
                        "not_started" if installed else "not_installed"
                    ),
                    "installed_version": "v7.2.95" if installed else None,
                    "verified": installed,
                    "listening": {"host": "127.0.0.1", "port": 15220}
                    if self.start_calls
                    else None,
                    "last_check": None,
                    "error_key": None,
                },
            }

        def restart_if_running(self) -> None:
            return None

        def note_installation_settled(self) -> None:
            return None

        def ensure_running(self) -> None:
            self.start_calls += 1

        def disable(self) -> None:
            self.disable_calls += 1

    async def run() -> None:
        installer = BlockingInstaller(tmp_path / "runtime")
        supervisor = Supervisor(installer)
        adapter = CLIProxyEngineAdapter(
            supervisor=supervisor,  # type: ignore[arg-type]
            state_store=supervisor.state_store,
        )
        continuation_ready = asyncio.Event()
        allow_continuation = asyncio.Event()
        if stop_wins:
            start_after_install = adapter._start_after_install

            async def gated_start_after_install(
                install_task: asyncio.Task[None],
            ) -> None:
                await asyncio.shield(install_task)
                continuation_ready.set()
                await allow_continuation.wait()
                await start_after_install(install_task)

            adapter._start_after_install = gated_start_after_install  # type: ignore[method-assign]

        installing = await adapter.install()
        assert installing.health is EngineHealth.INSTALLING
        assert await asyncio.to_thread(installer.started.wait, 2)

        deferred = await adapter.start()
        assert deferred.health is EngineHealth.INSTALLING
        assert supervisor.start_calls == 0

        installer.release.set()
        if stop_wins:
            await asyncio.wait_for(continuation_ready.wait(), timeout=2)
            stopped = await adapter.stop_runtime()
            allow_continuation.set()
            await asyncio.sleep(0)

            assert stopped.health is EngineHealth.NOT_STARTED
            assert supervisor.start_calls == 0
            assert supervisor.disable_calls == 1
            return

        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.OK:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("runtime did not start after installation")

        assert supervisor.start_calls == 1

    asyncio.run(run())


def test_runtime_install_failure_persists_closed_error_key(tmp_path: Path) -> None:
    class FailedInstaller(EngineRuntimeManager):
        def __init__(self, runtime_dir: Path) -> None:
            super().__init__(runtime_dir=runtime_dir, offline=True)
            self.ensure_calls = 0

        def ensure(
            self,
            *,
            force: bool = False,
            expected_target=None,
            on_resolved=None,
        ):
            del force
            assert expected_target is None
            self.ensure_calls += 1
            assert on_resolved is not None
            on_resolved(RUNTIME_INSTALL_TARGET)
            return {"ok": False, "changed": False, "reason": "fixture-secret"}

    async def run() -> None:
        installer = FailedInstaller(tmp_path / "runtime")
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        started = await adapter.install()
        assert started.health is EngineHealth.INSTALLING

        for _ in range(100):
            settled = await adapter.status()
            if settled.health is EngineHealth.NOT_INSTALLED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("failed installation did not settle")

        reloaded = FailedInstaller(tmp_path / "runtime").install_state()
        assert installer.ensure_calls == 1
        assert settled.error_key == "settings.models.install.fail.detail"
        assert reloaded is not None
        assert reloaded["state"] == "not_installed"
        assert reloaded["error_key"] == "settings.models.install.fail.detail"

    asyncio.run(run())


def test_runtime_install_failure_projects_terminal_when_settlement_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
    generation = _create_runtime_install_claim(installer)
    original_write = managed_runtime.write_json_atomic

    def fail_terminal_write(path: Path, payload: dict) -> None:
        if payload.get("state") == "not_installed":
            raise OSError("fixture settlement failure")
        original_write(path, payload)

    monkeypatch.setattr(managed_runtime, "write_json_atomic", fail_terminal_write)

    with pytest.raises(OSError, match="fixture settlement failure"):
        installer.transition_install_claim(
            InstallClaimTransition.SETTLE_FAILURE,
            generation=generation,
            target=RUNTIME_INSTALL_TARGET,
            reason="model_hub_engine_archive_download_failed",
        )

    projected = EngineSupervisor(
        installer=installer,
        state_store=EngineStateStore(tmp_path / "state"),
    ).status()["status"]
    assert projected["health"] == "not_installed"
    assert projected["error_key"] == "settings.models.install.fail.detail"
    assert not installer.install_state_path.exists()


def test_invalid_persisted_install_claim_fails_closed(tmp_path: Path) -> None:
    async def run() -> None:
        installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", offline=True)
        managed_runtime.write_json_atomic(
            installer.install_state_path,
            {
                "schema_version": 1,
                "state": "installing",
                "error_key": None,
                "target": {"runtime_version": "v7.2.95"},
            },
        )
        adapter = CLIProxyEngineAdapter(
            supervisor=EngineSupervisor(
                installer=installer,
                state_store=EngineStateStore(tmp_path / "state"),
            )
        )

        settled = await adapter.recover_installation()

        assert settled.health is EngineHealth.NOT_INSTALLED
        assert settled.error_key == "settings.models.install.fail.detail"
        persisted = installer.install_state()
        assert persisted is not None
        assert persisted["state"] == "not_installed"
        assert persisted["error_key"] == "settings.models.install.fail.detail"
        assert persisted["reason"] == "model_hub_engine_install_claim_invalid"

    asyncio.run(run())


def test_adapter_stream_outcome_commits_at_model_output_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        adapter = CLIProxyEngineAdapter(supervisor=supervisor, state_store=store)
        credential_ref = await adapter.provision_credential(
            "custom",
            "openai_chat",
            "upstream-secret",
            "https://api.example.test/v1",
        )
        await adapter.sync_sources([_binding(credential_ref)])
        await adapter.start()

        handle = await adapter.invoke("src_fixture123", "model-a", {}, True, "codex")
        assert handle.stream is not None
        body = b"".join([chunk async for chunk in handle.stream])
        assert body.startswith(b"event:")
        assert handle.outcome_available is True
        await handle.close_stream()
        await handle.close_stream()
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is False
        await adapter.stop()

    asyncio.run(run())


def test_engine_client_does_not_apply_a_total_turn_timeout(tmp_path: Path) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path)
        credential_ref = store.store_api_key(
            "upstream-secret",
            base_url="https://api.example.test/v1",
        )
        store.sync_sources([_binding(credential_ref, model_ids=("slow-stream",))])
        connection = supervisor.ensure_running()
        source = store.get_source("src_fixture123")
        assert source is not None

        handle = await EngineClient(connection, timeout=0.05).invoke(
            source,
            "slow-stream",
            {},
            stream=True,
        )
        assert handle.stream is not None
        body = b"".join([chunk async for chunk in handle.stream])
        assert b"chat.completion.chunk" in body
        assert b"[DONE]" in body
        assert (await handle.outcome()).kind is RawOutcomeKind.SUCCESS
        supervisor.stop()

    asyncio.run(run())


def test_engine_client_marks_loopback_stream_disconnect_as_engine_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        class Content:
            async def read(self, _size: int) -> bytes:
                return b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'

            async def iter_chunked(self, _size: int):
                raise client_module.aiohttp.ClientConnectionError("loopback engine closed")
                yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            client_module.aiohttp,
            "ClientSession",
            lambda **_kwargs: Session(),
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_chat",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection(
                base_url="http://127.0.0.1:15220",
                management_key="management-key",
                gateway_token="gateway-token",
            )
        ).invoke(source, "model-a", {}, stream=True)

        assert handle.stream is not None
        assert [chunk async for chunk in handle.stream] == [b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n']
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.error_code == "engine_down"
        assert outcome.stream_started is True

    asyncio.run(run())


@pytest.mark.parametrize(
    (
        "status",
        "reported_model",
        "expected_kind",
        "expected_code",
        "expected_reason",
    ),
    (
        (
            502,
            "source-fixture123/model-a",
            RawOutcomeKind.NETWORK_ERROR,
            "engine_down",
            None,
        ),
        (
            502,
            "source-fixture123/model-b",
            RawOutcomeKind.HTTP_ERROR,
            None,
            "server_error",
        ),
        (
            503,
            "source-fixture123/model-a",
            RawOutcomeKind.HTTP_ERROR,
            None,
            "server_error",
        ),
    ),
)
def test_engine_client_distinguishes_local_model_registration_failure(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reported_model: str,
    expected_kind: RawOutcomeKind,
    expected_code: str | None,
    expected_reason: str | None,
) -> None:
    async def run() -> None:
        payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"unknown provider for model {reported_model}",
                },
            }
        ).encode()

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return payload if self.reads == 1 else b""

        class Response:
            content = Content()
            headers = {"Content-Type": "application/json"}

            def __init__(self) -> None:
                self.status = status

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            client_module.aiohttp,
            "ClientSession",
            lambda **_: Session(),
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="anthropic",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )

        handle = await EngineClient(
            EngineConnection(
                "http://127.0.0.1:15220",
                "management",
                "gateway",
            )
        ).invoke(source, "model-a", {}, stream=False)
        outcome = await handle.outcome()
        decision = classify_outcome(outcome)

        assert outcome.kind is expected_kind
        assert outcome.error_code == expected_code
        assert decision.error_code == expected_code
        assert decision.reason == expected_reason

    asyncio.run(run())


@pytest.mark.parametrize(
    ("protocol", "output_chunk", "terminal_chunk"),
    [
        (
            "anthropic",
            b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ),
        (
            "openai_responses",
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed","sequence_number":4}\n\n',
        ),
        (
            "openai_chat",
            b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n',
            b"data: [DONE]\n\n",
        ),
    ],
)
def test_engine_client_keeps_served_after_terminal_marker_then_disconnect(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    output_chunk: bytes,
    terminal_chunk: bytes,
) -> None:
    async def run() -> None:
        class Content:
            async def read(self, _size: int) -> bytes:
                return output_chunk

            async def iter_chunked(self, _size: int):
                yield terminal_chunk
                raise client_module.aiohttp.ClientConnectionError("late disconnect")

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            client_module.aiohttp,
            "ClientSession",
            lambda **_kwargs: Session(),
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol=protocol,
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection(
                base_url="http://127.0.0.1:15220",
                management_key="management-key",
                gateway_token="gateway-token",
            )
        ).invoke(source, "model-a", {}, stream=True, request_protocol=protocol)

        assert handle.stream is not None
        assert [chunk async for chunk in handle.stream] == [output_chunk, terminal_chunk]
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is True

    asyncio.run(run())


@pytest.mark.parametrize("protocol", ("anthropic", "openai_responses", "openai_chat"))
def test_engine_client_classifies_buffered_2xx_native_error_before_success(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    async def run() -> None:
        payload = b'{"error":{"type":"rate_limit_error"}}'

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return payload if self.reads == 1 else b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "application/json"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol=protocol,
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=False, request_protocol=protocol)
        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.HTTP_ERROR
        assert outcome.error_type == "rate_limit_error"
        assert outcome.stream_started is False
        decision = classify_outcome(outcome)
        assert decision.action == "fallback"
        assert decision.reason == "rate_limited"

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid_chunk",
    (
        b"event: response.in_progress\ndata: {\n\n",
        b"event: response.in_progress\ndata: []\n\n",
        b'event: response.in_progress\ndata: {"type":"response.in_progress","sequence_number":1}\n\n',
        b"event: future.event\ndata: " + DEEP_JSON_ARRAY + b"\n\n",
    ),
)
def test_engine_client_transparently_forwards_unvalidated_stream_data(
    monkeypatch: pytest.MonkeyPatch,
    invalid_chunk: bytes,
) -> None:
    async def run() -> None:
        output = (
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
            b'"sequence_number":1}\n\n'
        )
        terminal = (
            b'event: response.completed\ndata: {"type":"response.completed","sequence_number":2}\n\n'
        )

        class Content:
            async def read(self, _size: int) -> bytes:
                return output

            async def iter_chunked(self, _size: int):
                yield invalid_chunk
                yield terminal

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=True)
        assert handle.stream is not None
        assert [chunk async for chunk in handle.stream] == [output, invalid_chunk, terminal]
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is True

    asyncio.run(run())


def test_engine_client_ignores_deep_json_before_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        unknown = b"event: future.event\ndata: " + DEEP_JSON_ARRAY + b"\n\n"
        terminal = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return unknown if self.reads == 1 else terminal if self.reads == 2 else b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=True)

        assert handle.stream is not None
        assert [chunk async for chunk in handle.stream] == [unknown + terminal]
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is False

    asyncio.run(run())


def test_engine_client_classifies_initial_stream_eof_as_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        class Content:
            async def read(self, _size: int) -> bytes:
                return b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=True)

        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.stream_started is False

    asyncio.run(run())


@pytest.mark.parametrize(
    ("event_type", "terminal_payload", "error_code", "expected_action", "expected_reason"),
    [
        (
            "response.failed",
            {"type": "response.failed", "response": {"error": {"code": "permission_error"}}},
            "permission_error",
            "surface",
            None,
        ),
        (
            "response.incomplete",
            {"type": "response.incomplete", "response": {"error": {"code": "permission_error"}}},
            "permission_error",
            "surface",
            None,
        ),
        (
            "error",
            {"type": "error", "code": "authentication_error"},
            "authentication_error",
            "refresh",
            None,
        ),
        (
            "error",
            {"type": "error", "code": "invalid_api_key"},
            "invalid_api_key",
            "refresh",
            None,
        ),
        (
            "error",
            {"type": "error", "code": "server_error"},
            "server_error",
            "fallback",
            "server_error",
        ),
    ],
)
def test_engine_client_recognizes_responses_failure_terminals(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    terminal_payload: dict[str, object],
    error_code: str,
    expected_action: str,
    expected_reason: str | None,
) -> None:
    async def run() -> None:
        terminal = json.dumps(
            terminal_payload,
            separators=(",", ":"),
        ).encode()

        class Content:
            async def read(self, _size: int) -> bytes:
                return b"event: " + event_type.encode() + b"\ndata: " + terminal + b"\n\n"

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=True
        )
        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.HTTP_ERROR
        assert outcome.error_code == error_code
        decision = classify_outcome(outcome)
        assert decision.action == expected_action
        assert decision.reason == expected_reason
        assert decision.downstream_status == (403 if error_code == "permission_error" else None)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("protocol", "event_name", "payload"),
    (
        (
            "openai_responses",
            "response.incomplete",
            {"type": "response.incomplete", "response": {"error": None}},
        ),
        (
            "openai_responses",
            "response.incomplete",
            {"type": "response.incomplete", "response": {"error": {}}},
        ),
    ),
)
def test_documented_incomplete_output_is_served_without_source_failure(
    protocol: str,
    event_name: str | None,
    payload: dict[str, object],
) -> None:
    wire_state = client_module.ProtocolSSEState(protocol)
    event = b"" if event_name is None else b"event: " + event_name.encode() + b"\n"
    wire_state.observe(event + b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n")
    source = SourceRecord(
        source_id="src_fixture123",
        vendor="custom",
        protocol=protocol,
        base_url="https://api.example.test/v1",
        credential_ref="cred_fixture123",
        allowed_origins=(),
        model_ids=("model-a",),
        prefix="source-fixture123",
    )
    outcome = client_module._observed_stream_terminal_outcome(
        wire_state,
        source,
        "model-a",
        200,
    )
    assert outcome is not None
    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert classify_outcome(outcome).action == "return"


@pytest.mark.parametrize(
    "finish_reason",
    ("stop", "length", "content_filter", "tool_calls", "function_call"),
)
def test_chat_finish_reason_is_not_a_wire_terminal(finish_reason: str) -> None:
    state = client_module.ProtocolSSEState("openai_chat")
    state.observe(
        b'data: {"choices":[{"finish_reason":"'
        + finish_reason.encode()
        + b'","delta":{}}]}\n\n'
    )
    assert state.terminal_outcome is None
    assert state.terminal_observation() is None
    state.observe(b"data: [DONE]\n\n")
    assert state.terminal_outcome == "served"


def test_downstream_close_after_chat_finish_reason_does_not_fabricate_success() -> None:
    async def run() -> None:
        first = b'data: {"choices":[{"finish_reason":"stop","delta":{}}]}\n\n'
        wire_state = client_module.ProtocolSSEState("openai_chat")
        wire_state.observe(first)

        class Content:
            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()

            def close(self) -> None:
                return None

        class Session:
            async def close(self) -> None:
                return None

        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_chat",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        outcome_future = asyncio.get_running_loop().create_future()
        prelude = client_module._StreamPrelude()
        prelude.write(first)
        stream = client_module._response_stream(
            response=Response(),
            session=Session(),
            prelude=prelude,
            source=source,
            model_id="model-a",
            protocol="openai_chat",
            outcome_future=outcome_future,
            wire_state=wire_state,
        )

        assert await anext(stream) == first
        await stream.aclose()
        assert outcome_future.done() is False

    asyncio.run(run())


def test_engine_client_keeps_success_after_a_later_complete_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        first = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n'
        terminal = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'
        extra = b'data: {"type":"response.output_text.delta"}\n\n'

        class Content:
            async def read(self, _size: int) -> bytes:
                return first

            async def iter_chunked(self, _size: int):
                yield terminal
                yield extra

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=True
        )
        assert handle.stream is not None
        assert [chunk async for chunk in handle.stream] == [first, terminal, extra]
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS

    asyncio.run(run())


@pytest.mark.parametrize(
    ("event_name", "payload_type"),
    [
        (None, "response.completed"),
        ("response.failed", "response.completed"),
        ("response.completed", "response.failed"),
    ],
)
def test_engine_client_requires_terminal_event_name_and_payload_identity(
    monkeypatch: pytest.MonkeyPatch,
    event_name: str | None,
    payload_type: str,
) -> None:
    async def run() -> None:
        event_line = b"" if event_name is None else f"event: {event_name}\n".encode()
        terminal = event_line + f'data: {{"type":"{payload_type}"}}\n\n'.encode()

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return terminal if self.reads == 1 else b""

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=True
        )
        assert handle.stream is None
        assert (await handle.outcome()).kind is RawOutcomeKind.NETWORK_ERROR

    asyncio.run(run())


def test_engine_client_transparently_reads_non_sse_stream_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return (
                    b'{"error":{"type":"server_error"}}'
                    if self.reads == 1
                    else b""
                )

        content = Content()

        class Response:
            status = 200
            headers = {"Content-Type": "application/json; charset=utf-8"}

            def __init__(self) -> None:
                self.content = content

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=True
        )
        assert handle.stream is None
        assert content.reads == 2
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.stream_started is False

    asyncio.run(run())


@pytest.mark.parametrize(
    ("protocol", "first", "expected_kind"),
    [
        (
            "anthropic",
            b'event: error\ndata: {"type":"error","error":{"type":"permission_error","message":"denied"}}\n\n',
            RawOutcomeKind.HTTP_ERROR,
        ),
        (
            "openai_responses",
            b'event: error\ndata: {"type":"error","code":"permission_error",'
            b'"message":"denied","param":null,"sequence_number":1}\n\n',
            RawOutcomeKind.HTTP_ERROR,
        ),
        (
            "openai_chat",
            b'data: {"object":"chat.completion.chunk","error":'
            b'{"type":"permission_error","message":"denied"},"choices":[]}\n\n',
            RawOutcomeKind.HTTP_ERROR,
        ),
        (
            "openai_chat",
            b'data: {"object":"chat.completion.chunk","choices":[]}\n\n',
            RawOutcomeKind.NETWORK_ERROR,
        ),
    ],
)
def test_engine_client_requires_a_protocol_terminal_event_before_clean_eof(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    first: bytes,
    expected_kind: RawOutcomeKind,
) -> None:
    async def run() -> None:
        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return first if self.reads == 1 else b""

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(
            client_module.aiohttp,
            "ClientSession",
            lambda **_kwargs: Session(),
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol=protocol,
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection(
                base_url="http://127.0.0.1:15220",
                management_key="management-key",
                gateway_token="gateway-token",
            )
        ).invoke(source, "model-a", {}, stream=True, request_protocol=protocol)

        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is expected_kind
        decision = classify_outcome(outcome)
        if expected_kind is RawOutcomeKind.HTTP_ERROR:
            assert "permission_error" in outcome.error_candidates
            assert decision.downstream_status == 403
            assert terminal_outcome_category(outcome, decision) == "request_nonfallback"
        else:
            assert outcome.error_code is None
            assert decision.reason == "network"

    asyncio.run(run())


@pytest.mark.parametrize("invalid_type", [None, [], {}])
def test_engine_client_ignores_non_string_stream_event_types(
    monkeypatch: pytest.MonkeyPatch,
    invalid_type: object,
) -> None:
    async def run() -> None:
        first = (
            b"event: response.completed\ndata: "
            + json.dumps({"type": invalid_type}, separators=(",", ":")).encode()
            + b"\n\n"
        )

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return first if self.reads == 1 else b""

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=True
        )
        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.error_code is None

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["connect", "first_byte"])
def test_engine_client_cancellation_closes_pre_handle_resources(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    async def run() -> None:
        reached = asyncio.Event()
        never = asyncio.Event()

        class Content:
            async def read(self, _size: int) -> bytes:
                reached.set()
                await never.wait()
                return b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        response = Response()

        class Session:
            def __init__(self, **_kwargs) -> None:
                self.close_calls = 0

            async def post(self, *_args, **_kwargs):
                if phase == "connect":
                    reached.set()
                    await never.wait()
                return response

            async def close(self) -> None:
                self.close_calls += 1

        session = Session()
        monkeypatch.setattr(
            client_module.aiohttp,
            "ClientSession",
            lambda **_kwargs: session,
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_chat",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        client = EngineClient(
            EngineConnection(
                base_url="http://127.0.0.1:15220",
                management_key="management-key",
                gateway_token="gateway-token",
            )
        )

        task = asyncio.create_task(client.invoke(source, "model-a", {}, stream=True))
        await asyncio.wait_for(reached.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        assert session.close_calls == 1
        assert response.close_calls == (1 if phase == "first_byte" else 0)

    asyncio.run(run())


def test_buffered_projection_drains_before_its_spool_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        projection_started = threading.Event()
        release_projection = threading.Event()
        preludes: list[TrackingPrelude] = []

        class TrackingPrelude(client_module._StreamPrelude):
            def __init__(self) -> None:
                super().__init__(memory_limit=1)
                preludes.append(self)

        def project(_protocol, reader, **_kwargs):
            projection_started.set()
            assert release_projection.wait(timeout=1)
            assert not reader.closed
            return client_module.ProtocolObservation(outcome="served")

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return b'{"output":[]}' if self.reads == 1 else b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "application/json"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module, "_StreamPrelude", TrackingPrelude)
        monkeypatch.setattr(client_module, "observe_buffered_protocol_response", project)
        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        client = EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        )

        task = asyncio.create_task(client.invoke(source, "model-a", {}, stream=False))
        assert await asyncio.to_thread(projection_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert preludes and not preludes[0].closed
        release_projection.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert preludes[0].closed

    asyncio.run(run())


def test_closing_an_unstarted_stream_publishes_an_observed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        first = (
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
            b'"delta":"ok"}\n\nevent: response.completed\ndata: {"type":"response.completed",'
            b'"response":{"usage":{"input_tokens":12,"output_tokens":3}}}\n\n'
        )

        class Content:
            async def read(self, _size: int) -> bytes:
                return first

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )

        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=True)

        assert handle.stream is not None
        assert handle.outcome_available is False
        await handle.close_stream()
        assert handle.outcome_available is True
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.usage == ProtocolUsageReport.of(
            input_tokens=12,
            cached_input_tokens=0,
            output_tokens=3,
        )

    asyncio.run(run())


def test_stream_replay_failure_preserves_observed_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        class FailingPrelude(client_module._StreamPrelude):
            def __init__(self) -> None:
                super().__init__(memory_limit=1)

            def write(self, data: bytes) -> None:
                raise OSError("temporary storage unavailable")

        first = (
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"usage":{"input_tokens":77,"output_tokens":1}}}\n\n'
        )

        class Content:
            async def read(self, _size: int) -> bytes:
                return first

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module, "_StreamPrelude", FailingPrelude)
        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="anthropic",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )

        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(
            source,
            "model-a",
            {},
            stream=True,
            request_protocol="anthropic",
        )

        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.NETWORK_ERROR
        assert outcome.error_code == "engine_down"
        assert outcome.usage == ProtocolUsageReport.of(
            input_tokens=77,
            cached_input_tokens=0,
            output_tokens=1,
        )

    asyncio.run(run())


def test_slow_source_prelude_is_bounded_without_blocking_other_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        blocked = asyncio.Event()
        never = asyncio.Event()
        preludes: list[TrackingPrelude] = []
        slow_keepalive = b":" + b"k" * 256 + b"\n\n"
        fast_output = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n'
        fast_terminal = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'

        class TrackingPrelude(client_module._StreamPrelude):
            def __init__(self) -> None:
                super().__init__(memory_limit=64)
                self.physical_close_calls = 0
                preludes.append(self)

            def close(self) -> None:
                if not self.closed:
                    self.physical_close_calls += 1
                super().close()

        class Content:
            def __init__(self, source_id: str) -> None:
                self.source_id = source_id
                self.reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                if self.source_id == "src_slow0001":
                    if self.reads == 1:
                        return slow_keepalive
                    blocked.set()
                    await never.wait()
                    return b""
                return fast_output

            async def iter_chunked(self, _size: int):
                yield fast_terminal

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self, source_id: str) -> None:
                self.content = Content(source_id)

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, json=None, **_kwargs):
                source_id = "src_slow0001" if str(json["model"]).startswith("slow/") else "src_fast0001"
                return Response(source_id)

            async def close(self) -> None:
                return None

        class Store:
            def __init__(self, sources: tuple[SourceRecord, ...]) -> None:
                self.sources = {source.source_id: source for source in sources}

            def get_source(self, source_id: str) -> SourceRecord | None:
                return self.sources.get(source_id)

        class Supervisor:
            def __init__(self, client: EngineClient) -> None:
                self._client = client

            def client(self) -> EngineClient:
                return self._client

        slow = SourceRecord(
            source_id="src_slow0001",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://slow.example.test/v1",
            credential_ref="cred_slow0001",
            allowed_origins=("codex",),
            model_ids=("model-a",),
            prefix="slow",
        )
        fast = SourceRecord(
            source_id="src_fast0001",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://fast.example.test/v1",
            credential_ref="cred_fast0001",
            allowed_origins=("codex",),
            model_ids=("model-a",),
            prefix="fast",
        )
        monkeypatch.setattr(client_module, "_StreamPrelude", TrackingPrelude)
        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        client = EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway"))
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(client),  # type: ignore[arg-type]
            state_store=Store((slow, fast)),  # type: ignore[arg-type]
        )

        slow_task = asyncio.create_task(adapter.invoke(slow.source_id, "model-a", {}, True, "codex"))
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert preludes[0].spilled is True
        assert preludes[0].in_memory_bytes == 0

        fast_handle = await asyncio.wait_for(
            adapter.invoke(fast.source_id, "model-a", {}, True, "codex"),
            timeout=1,
        )
        assert fast_handle.stream is not None
        chunks = [chunk async for chunk in fast_handle.stream]
        assert b"".join(chunks) == fast_output + fast_terminal
        assert b"".join([chunk async for chunk in preludes[0].chunks()]) == slow_keepalive

        slow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_task
        assert preludes[0].physical_close_calls == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error_type", "error_code"),
    [
        ("permission_error", "api_error"),
        ("api_error", "permission_error"),
        ("permission_error", "permission_error"),
        ("api_error", "api_error"),
    ],
)
def test_engine_error_fields_preserve_nested_candidates(
    error_type: str,
    error_code: str,
) -> None:
    payload = json.dumps({"error": {"type": error_type, "code": error_code}}).encode()

    raw_type, raw_code, candidates = client_module._raw_error_fields(payload)

    assert raw_type == error_type
    assert raw_code == error_code
    assert set(candidates) == {error_type, error_code}
    decision = classify_outcome(
        client_module._outcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            source=SourceRecord(
                source_id="src_fixture123",
                vendor="custom",
                protocol="openai_chat",
                base_url="https://api.example.test/v1",
                credential_ref="cred_fixture123",
                allowed_origins=(),
                model_ids=("model-a",),
                prefix="source-fixture123",
            ),
            model_id="model-a",
            http_status=503,
            error_type=raw_type,
            error_code=raw_code,
            error_candidates=candidates,
            message="permission denied",
        )
    )
    if "permission_error" in {error_type, error_code}:
        assert decision.action == "surface"
        assert decision.error_code == "request_incompatible"
        assert decision.downstream_status == 403
    else:
        assert decision.action == "fallback"
        assert decision.reason == "server_error"


def test_engine_error_fields_ignore_machine_codes_outside_the_trusted_envelope() -> None:
    payload = json.dumps(
        {
            "error": {"type": "api_error"},
            "request": {"type": "permission_error"},
            "metadata": {"code": "permission_error"},
        }
    ).encode()

    raw_type, raw_code, candidates = client_module._raw_error_fields(payload)

    assert raw_type == "api_error"
    assert raw_code is None
    assert candidates == ("api_error",)


@pytest.mark.parametrize(
    ("phase", "stream", "expected_kind", "expected_status", "stream_started"),
    [
        ("first_byte", True, RawOutcomeKind.TIMEOUT, None, False),
        ("error_body", True, RawOutcomeKind.HTTP_ERROR, 429, False),
        ("non_stream", False, RawOutcomeKind.TIMEOUT, 200, False),
    ],
)
def test_engine_client_times_out_before_completion(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    stream: bool,
    expected_kind: RawOutcomeKind,
    expected_status: int | None,
    stream_started: bool,
) -> None:
    async def run() -> None:
        blocked_phase = asyncio.Event()
        never_release = asyncio.Event()

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                if phase == "non_stream" and self.reads == 1:
                    return b"{"
                blocked_phase.set()
                await never_release.wait()
                return b""

        class Response:
            status = 429 if phase == "error_body" else 200
            content = Content()
            headers = {"Content-Type": "text/event-stream"}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_chat",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway"),
            timeout=0.01,
        ).invoke(
            source,
            "model-a",
            {},
            stream=stream,
        )

        assert blocked_phase.is_set()
        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is expected_kind
        assert outcome.http_status == expected_status
        assert outcome.stream_started is stream_started

    asyncio.run(run())


@pytest.mark.parametrize(
    "model_id",
    ["invalid-json", "oversized-non-stream"],
)
def test_engine_client_non_stream_response_size_does_not_change_protocol_outcome(
    tmp_path: Path,
    model_id: str,
) -> None:
    async def run() -> None:
        supervisor, store = _fixture_supervisor(tmp_path / model_id)
        credential_ref = store.store_api_key(
            "upstream-secret",
            base_url="https://api.example.test/v1",
        )
        store.sync_sources([_binding(credential_ref, model_ids=(model_id,))])
        source = store.get_source("src_fixture123")
        assert source is not None
        connection = supervisor.ensure_running()

        handle = await EngineClient(connection).invoke(
            source,
            model_id,
            {},
            stream=False,
        )

        assert handle.stream is not None
        assert b"".join([chunk async for chunk in handle.stream])
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.http_status == 200
        assert outcome.stream_started is True
        supervisor.stop()

    asyncio.run(run())


@pytest.mark.parametrize("case", STREAM_TRANSPORT_BOUNDARIES, ids=lambda case: case["name"])
def test_engine_client_preserves_large_valid_responses(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, object],
) -> None:
    async def run() -> None:
        large_value = b"x" * (2 * 1024 * 1024)
        payload = (
            b'{"output":[{"content":"' + large_value + b'"}]}'
            if not case["stream"]
            else (
                b"event: response.image_generation_call.partial_image\n"
                b'data: {"type":"response.image_generation_call.partial_image",'
                b'"partial_image_b64":"'
                + large_value
                + b'"}\n\nevent: response.completed\ndata: {"type":"response.completed"}\n\n'
            )
        )

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return payload if self.reads == 1 else b""

            async def iter_chunked(self, _size: int):
                if False:
                    yield b""

        class Response:
            status = 200
            content = Content()
            headers = {"Content-Type": ("text/event-stream" if case["stream"] else "application/json")}

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        handle = await EngineClient(EngineConnection("http://127.0.0.1:15220", "management", "gateway")).invoke(
            source, "model-a", {}, stream=bool(case["stream"])
        )

        assert handle.stream is not None
        chunks = [chunk async for chunk in handle.stream]
        outcome = await handle.outcome()
        assert outcome.kind.value == case["expected_outcome"]
        assert b"".join(chunks) == payload

    asyncio.run(run())


@pytest.mark.parametrize("status", (200, 503))
def test_engine_client_projects_machine_errors_from_large_buffered_bodies(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    async def run() -> None:
        payload = json.dumps(
            {
                "error": {
                    "diagnostic": "x" * (2 * 1024 * 1024),
                    "type": "permission_error",
                }
            },
            separators=(",", ":"),
        ).encode()

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return payload if self.reads == 1 else b""

        class Response:
            content = Content()
            headers = {"Content-Type": "application/json"}

            def __init__(self) -> None:
                self.status = status

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )

        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway")
        ).invoke(source, "model-a", {}, stream=False)

        assert handle.stream is None
        outcome = await handle.outcome()
        assert outcome.kind is RawOutcomeKind.HTTP_ERROR
        assert outcome.error_type == "permission_error"
        assert outcome.error_candidates == ("permission_error",)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "expected_kind"),
    ((200, RawOutcomeKind.TIMEOUT), (503, RawOutcomeKind.HTTP_ERROR)),
)
def test_buffered_response_projection_uses_the_response_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_kind: RawOutcomeKind,
) -> None:
    async def run() -> None:
        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return b"{}" if self.reads == 1 else b""

        class Response:
            content = Content()
            headers = {"Content-Type": "application/json"}

            def __init__(self) -> None:
                self.status = status

            def close(self) -> None:
                return None

        class Session:
            async def post(self, *_args, **_kwargs):
                return Response()

            async def close(self) -> None:
                return None

        projection_deadlines: list[float] = []

        def project_before_deadline(_reader, _projector, *, deadline):
            projection_deadlines.append(deadline)
            raise asyncio.TimeoutError("buffered response exceeded its request deadline")

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        monkeypatch.setattr(
            client_module,
            "_project_before_deadline",
            project_before_deadline,
        )
        source = SourceRecord(
            source_id="src_fixture123",
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            credential_ref="cred_fixture123",
            allowed_origins=(),
            model_ids=("model-a",),
            prefix="source-fixture123",
        )
        started = time.monotonic()

        handle = await EngineClient(
            EngineConnection("http://127.0.0.1:15220", "management", "gateway"),
            timeout=1.0,
        ).invoke(source, "model-a", {}, stream=False)

        assert (await handle.outcome()).kind is expected_kind
        assert projection_deadlines == pytest.approx([started + 1.0], abs=0.1)

    asyncio.run(run())


def test_model_discovery_accepts_large_valid_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        payload = json.dumps(
            {"data": [{"id": "model-a", "metadata": "x" * (5 * 1024 * 1024)}]},
            separators=(",", ":"),
        ).encode()

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                if _size == 0:
                    return b""
                self.reads += 1
                return payload if self.reads == 1 else b""

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return Response()

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())

        assert await client_module.probe_models(
            vendor="custom",
            protocol="openai_responses",
            base_url="https://api.example.test/v1",
            secret="secret",
        ) == (DiscoveredModel(id="model-a"),)

    asyncio.run(run())


def test_model_discovery_projection_uses_the_request_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return b"{}" if self.reads == 1 else b""

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return Response()

        projection_deadlines: list[float] = []

        def project_before_deadline(_reader, _projector, *, deadline):
            projection_deadlines.append(deadline)
            raise asyncio.TimeoutError("model discovery exceeded its request deadline")

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        monkeypatch.setattr(
            client_module,
            "_project_before_deadline",
            project_before_deadline,
        )
        started = time.monotonic()

        with pytest.raises(EngineClientError) as caught:
            await client_module.probe_models(
                vendor="custom",
                protocol="openai_responses",
                base_url="https://api.example.test/v1",
                secret="secret",
                timeout=1.0,
            )

        assert caught.value.error_type == "timeout"
        assert projection_deadlines == pytest.approx([started + 1.0], abs=0.1)

    asyncio.run(run())


def test_model_discovery_translates_local_spool_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        payload = b"x" * (client_module._PRELUDE_MEMORY_BYTES + 1)

        class Content:
            reads = 0

            async def read(self, _size: int) -> bytes:
                self.reads += 1
                return payload if self.reads == 1 else b""

        class Response:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def get(self, *_args, **_kwargs):
                return Response()

        class UnavailableSpool:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write(self, data: bytes) -> None:
                assert len(data) > client_module._PRELUDE_MEMORY_BYTES
                raise OSError("temporary storage unavailable")

        monkeypatch.setattr(client_module.aiohttp, "ClientSession", lambda **_: Session())
        monkeypatch.setattr(
            client_module.tempfile,
            "SpooledTemporaryFile",
            lambda **_: UnavailableSpool(),
        )

        with pytest.raises(EngineClientError) as caught:
            await client_module.probe_models(
                vendor="custom",
                protocol="openai_responses",
                base_url="https://api.example.test/v1",
                secret="secret",
            )

        assert str(caught.value) == "model discovery failed"
        assert caught.value.error_type == "OSError"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("vendor", "endpoint"),
    [
        ("anthropic", "/anthropic-auth-url"),
        ("openai", "/codex-auth-url"),
        ("codex", "/codex-auth-url"),
    ],
)
def test_oauth_start_uses_webui_callback_for_observable_vendors(
    tmp_path: Path,
    vendor: str,
    endpoint: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.start_query = None

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                return {"files": []}
            if path == endpoint:
                self.start_query = query
                return {"state": "engine-state", "url": "https://example.test/oauth"}
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, client),  # type: ignore[arg-type]
            state_store=store,
        )

        flow = await adapter.start_oauth("src_fixture123", vendor)

        assert client.start_query == {"is_webui": "true"}
        assert flow.expects == "paste_callback_url"

    asyncio.run(run())


@pytest.mark.parametrize("vendor", ["antigravity", "kimi", "xai"])
def test_oauth_start_rejects_engine_only_vendors_before_engine_work(
    tmp_path: Path,
    vendor: str,
) -> None:
    class Supervisor:
        def client(self):
            raise AssertionError("unsupported Model Hub OAuth must not reach the engine")

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(),  # type: ignore[arg-type]
            state_store=store,
        )

        with pytest.raises(
            EngineStateError,
            match="lacks Model Hub response-backed observation",
        ):
            await adapter.start_oauth("src_fixture123", vendor)

    asyncio.run(run())


def test_oauth_model_discovery_accepts_engine_definition_fields(tmp_path: Path) -> None:
    class Client:
        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            assert (method, path) == ("GET", "/auth-files/models")
            assert query == {"name": "claude-account.json"}
            return {
                "models": [
                    {
                        "id": "model-id",
                        "alias": "ignored-alias",
                        "supported_parameters": [
                            "reasoning",
                            "temperature",
                            "reasoning",
                        ],
                    },
                    {
                        "alias": "model-alias",
                        "name": "ignored-name",
                        "supported_parameters": ["reasoning", 7],
                    },
                    {"name": "model-name", "supported_parameters": []},
                    {
                        "id": "model-id",
                        "supported_parameters": ["ignored-duplicate"],
                    },
                ]
            }

    class Supervisor:
        def __init__(self, store: EngineStateStore) -> None:
            self.state_store = store

        def client(self):
            return Client()

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        (store.auth_dir / "claude-account.json").write_text("{}", encoding="utf-8")
        credential_ref = store.bind_oauth_credential(
            "src_fixture123",
            "anthropic",
            "claude-account.json",
        )
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store),  # type: ignore[arg-type]
            state_store=store,
        )

        models = await adapter.discover_models(
            "anthropic",
            "anthropic",
            None,
            credential_ref,
        )

        assert models == (
            DiscoveredModel(
                id="model-id",
                supported_parameters=("reasoning", "temperature"),
            ),
            DiscoveredModel(id="model-alias"),
            DiscoveredModel(id="model-name", supported_parameters=()),
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "oauth_record_case",
    [
        "new",
        "refresh",
        "conflict",
        "duplicate_binding",
        "metadata_failure",
        "patch_failure",
        "new_patch_failure",
        "new_patch_engine_delete_failure",
        "new_patch_local_delete_failure",
        "new_patch_revoke_failure",
    ],
)
def test_oauth_flow_handles_new_refreshed_and_conflicting_auth_records(
    tmp_path: Path,
    oauth_record_case: str,
) -> None:
    class Client:
        def __init__(self) -> None:
            self.auth_calls = 0
            self.patches: list[dict[str, object]] = []
            self.deletes: list[str] = []

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                if method == "DELETE":
                    if oauth_record_case == "new_patch_engine_delete_failure":
                        raise EngineClientError("delete failed")
                    self.deletes.append(str((query or {}).get("name")))
                    return {"status": "ok"}
                self.auth_calls += 1
                if self.auth_calls == 1:
                    if oauth_record_case in {
                        "new",
                        "metadata_failure",
                        "new_patch_failure",
                        "new_patch_engine_delete_failure",
                        "new_patch_local_delete_failure",
                        "new_patch_revoke_failure",
                    }:
                        return {"files": []}
                    return {
                        "files": [
                            {
                                "id": "claude-account.json",
                                "name": "claude-account.json",
                                "provider": "claude",
                                "modtime": "2026-07-23T04:00:00Z",
                            }
                        ]
                    }
                return {
                    "files": [
                        {
                            "id": "claude-account.json",
                            "name": "claude-account.json",
                            "provider": "claude",
                            "modtime": "2026-07-23T04:01:00Z",
                        }
                    ]
                }
            if path == "/anthropic-auth-url":
                return {"state": "engine-state", "url": "https://example.test/oauth"}
            if path == "/get-auth-status":
                return {"status": "ok"}
            if path == "/auth-files/fields":
                if oauth_record_case in {
                    "patch_failure",
                    "new_patch_failure",
                    "new_patch_engine_delete_failure",
                    "new_patch_local_delete_failure",
                    "new_patch_revoke_failure",
                }:
                    raise EngineClientError("patch failed")
                self.patches.append(dict(payload or {}))
                return {"status": "ok"}
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client(self):
            return self._client

        def client_if_running(self):
            return None

        def invalidate_configs(self) -> None:
            self.state_store.clear_runtime_configs()

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        store.prepare_instance("install-1")
        (store.auth_dir / "claude-account.json").write_text("{}", encoding="utf-8")
        (store.auth_dir / "claude-account.json").chmod(0o600)
        existing_ref = None
        existing_prefix = None
        if oauth_record_case not in {
            "new",
            "metadata_failure",
            "new_patch_failure",
            "new_patch_engine_delete_failure",
            "new_patch_local_delete_failure",
            "new_patch_revoke_failure",
        }:
            existing_ref = store.bind_oauth_credential(
                "src_other1234" if oauth_record_case == "conflict" else "src_fixture123",
                "anthropic",
                "claude-account.json",
            )
            existing_prefix = store.credential_metadata(existing_ref)["prefix"]
            if oauth_record_case == "duplicate_binding":
                duplicate_path = store._credential_path(f"cred_{'f' * 32}")
                duplicate_path.write_bytes(store._credential_path(existing_ref).read_bytes())
                duplicate_path.chmod(0o600)
            if oauth_record_case == "patch_failure":
                store.sync_sources(
                    [
                        _binding(
                            existing_ref,
                            vendor="anthropic",
                            protocol="anthropic",
                            base_url=None,
                            allowed_origins=("claude",),
                        )
                    ]
                )

        original_metadata = store.credential_metadata
        original_local_delete = store.delete_oauth_auth_file
        original_revoke = store.revoke_credential
        revoke_calls: list[str] = []

        def credential_metadata(credential_ref: str):
            payload = original_metadata(credential_ref)
            if oauth_record_case == "metadata_failure" and payload.get("source_id") == "src_fixture123":
                raise EngineStateError("metadata read failed")
            return payload

        def delete_oauth_auth_file(auth_name: str) -> None:
            if oauth_record_case == "new_patch_local_delete_failure":
                raise EngineStateError("local delete failed")
            original_local_delete(auth_name)

        def revoke_credential(credential_ref: str) -> None:
            revoke_calls.append(credential_ref)
            if oauth_record_case == "new_patch_revoke_failure":
                raise EngineStateError("revoke failed")
            original_revoke(credential_ref)

        store.credential_metadata = credential_metadata  # type: ignore[method-assign]
        store.delete_oauth_auth_file = delete_oauth_auth_file  # type: ignore[method-assign]
        store.revoke_credential = revoke_credential  # type: ignore[method-assign]
        client = Client()
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, client),  # type: ignore[arg-type]
            state_store=store,
        )

        flow = await adapter.start_oauth("src_fixture123", "anthropic")
        with pytest.raises(EngineStateError, match="already active"):
            await adapter.start_oauth("src_other1234", "anthropic")
        completed, concurrent = await asyncio.gather(
            adapter.oauth_status(flow.flow_id),
            adapter.oauth_status(flow.flow_id),
        )

        if oauth_record_case == "conflict":
            assert completed.state == "failed"
            assert completed.error_key == "models.oauth.binding_failed"
            assert completed.channel == "hub"
            assert completed.retained_material_disposition is RetainedMaterialDisposition.FOREIGN_SOURCE_REF
            assert completed.retained_credential_ref is None
            assert concurrent.state == "failed"
            retry = await adapter.start_oauth("src_fixture123", "anthropic")
            assert retry.state == "awaiting_action"
            assert not client.patches
            return

        if oauth_record_case == "duplicate_binding":
            assert completed.state == "failed"
            assert completed.retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
            assert completed.retained_credential_ref is None
            return

        if oauth_record_case == "metadata_failure":
            assert completed.state == "failed"
            assert completed.retained_material_disposition is RetainedMaterialDisposition.FLOW_SOURCE_REF
            assert completed.retained_credential_ref is not None
            assert completed.credential_ref is None
            return

        if oauth_record_case in {
            "patch_failure",
            "new_patch_failure",
            "new_patch_engine_delete_failure",
            "new_patch_local_delete_failure",
            "new_patch_revoke_failure",
        }:
            assert completed.state == "failed"
            assert completed.error_key == "models.oauth.binding_failed"
            assert concurrent.state == "failed"
            if oauth_record_case == "patch_failure":
                assert store.credential_metadata(existing_ref)["prefix"] == existing_prefix
                assert (store.auth_dir / "claude-account.json").exists()
                assert not client.deletes
                assert completed.retained_material_disposition is RetainedMaterialDisposition.FLOW_SOURCE_REF
                assert completed.retained_credential_ref == existing_ref
            elif oauth_record_case == "new_patch_failure":
                assert client.deletes == ["claude-account.json"]
                assert not (store.auth_dir / "claude-account.json").exists()
                assert not list((store.root / "credentials").glob("*.json"))
                assert completed.retained_material_disposition is RetainedMaterialDisposition.NONE
                assert completed.retained_credential_ref is None
                assert len(revoke_calls) == 1
            else:
                assert completed.retained_material_disposition is RetainedMaterialDisposition.ORPHAN_REF
                assert completed.retained_credential_ref is not None
                assert store._credential_path(completed.retained_credential_ref).exists()
                if oauth_record_case in {
                    "new_patch_engine_delete_failure",
                    "new_patch_local_delete_failure",
                }:
                    assert revoke_calls == []
                else:
                    assert revoke_calls == [completed.retained_credential_ref]
            retry = await adapter.start_oauth("src_fixture123", "anthropic")
            assert retry.state == "awaiting_action"
            return

        assert completed.state == "success"
        assert completed.channel == "hub"
        assert completed.source_id == "src_fixture123"
        assert completed.credential_ref and completed.credential_ref.startswith("cred_")
        assert completed.retained_material_disposition is RetainedMaterialDisposition.FLOW_SOURCE_REF
        assert completed.retained_credential_ref == completed.credential_ref
        if oauth_record_case == "refresh":
            assert completed.credential_ref == existing_ref
        assert concurrent.credential_ref == completed.credential_ref
        assert client.patches[0]["name"] == "claude-account.json"
        assert str(client.patches[0]["prefix"]).startswith("avibe-")
        if oauth_record_case == "refresh":
            assert client.patches[0]["prefix"] == existing_prefix
            assert len(list((store.root / "credentials").glob("*.json"))) == 1
        repeated = await adapter.oauth_status(flow.flow_id)
        assert repeated.credential_ref == completed.credential_ref
        assert len(client.patches) == 1
        await adapter.cancel_oauth(flow.flow_id)
        assert (await adapter.oauth_status(flow.flow_id)).state == "success"
        await adapter.revoke_credential(completed.credential_ref)
        assert not (store.auth_dir / "claude-account.json").exists()
        with pytest.raises(EngineStateError, match="unavailable"):
            store.credential_metadata(completed.credential_ref)

    asyncio.run(run())


def test_oauth_flow_releases_provider_after_engine_failure_or_expiry(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.starts = 0

        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                return {"files": []}
            if path == "/anthropic-auth-url":
                self.starts += 1
                return {
                    "state": f"engine-state-{self.starts}",
                    "url": "https://example.test/oauth",
                }
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client
            self.unavailable = False

        def client(self):
            if self.unavailable:
                raise EngineUnavailableError("engine unavailable")
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        client = Client()
        supervisor = Supervisor(store, client)
        adapter = CLIProxyEngineAdapter(
            supervisor=supervisor,  # type: ignore[arg-type]
            state_store=store,
        )

        with pytest.raises(
            EngineStateError,
            match="lacks Model Hub response-backed observation",
        ):
            await adapter.start_oauth("src_fixture123", "gemini")

        failed_flow = await adapter.start_oauth("src_fixture123", "anthropic")
        supervisor.unavailable = True
        failed = await adapter.oauth_status(failed_flow.flow_id)
        assert failed.state == "failed"
        assert failed.error_key == "models.oauth.engine_unavailable"
        assert failed.retained_material_disposition is RetainedMaterialDisposition.NONE
        assert failed.retained_credential_ref is None

        supervisor.unavailable = False
        expiring_flow = await adapter.start_oauth("src_other1234", "anthropic")
        adapter._oauth_flows[expiring_flow.flow_id].expires_at_iso = "2000-01-01T00:00:00+00:00"
        replacement = await adapter.start_oauth("src_third1234", "anthropic")
        assert replacement.state == "awaiting_action"
        expired = await adapter.oauth_status(expiring_flow.flow_id)
        assert expired.state == "failed"
        assert expired.error_key == "models.oauth.expired"
        assert expired.retained_material_disposition is RetainedMaterialDisposition.NONE
        assert expired.retained_credential_ref is None

    asyncio.run(run())


def test_oauth_terminal_uncertainty_never_claims_cleanup(tmp_path: Path) -> None:
    class Client:
        def management_request(self, method, path, *, query=None, payload=None, timeout=None):
            if path == "/auth-files":
                return {"files": []}
            if path == "/anthropic-auth-url":
                return {"state": "browser-state", "url": "https://example.test/oauth"}
            if path == "/codex-auth-url":
                return {
                    "state": "device-state",
                    "flow": "device",
                    "user_code": "ABCD-EFGH",
                    "url": "https://example.test/device",
                }
            if path == "/oauth-callback":
                raise EngineClientError("response lost after submission")
            raise AssertionError((method, path, query, payload, timeout))

    class Supervisor:
        def __init__(self, store: EngineStateStore, client: Client) -> None:
            self.state_store = store
            self._client = client

        def client(self):
            return self._client

    async def run() -> None:
        store = EngineStateStore(tmp_path / "state")
        adapter = CLIProxyEngineAdapter(
            supervisor=Supervisor(store, Client()),  # type: ignore[arg-type]
            state_store=store,
        )

        browser = await adapter.start_oauth("src_fixture123", "anthropic")
        failed = await adapter.submit_oauth(browser.flow_id, "callback-code")
        assert failed.state == "failed"
        assert failed.retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
        assert failed.retained_credential_ref is None

        device = await adapter.start_oauth("src_other1234", "openai")
        adapter._oauth_flows[device.flow_id].expires_at_iso = "2000-01-01T00:00:00+00:00"
        expired = await adapter.oauth_status(device.flow_id)
        assert expired.state == "failed"
        assert expired.retained_material_disposition is RetainedMaterialDisposition.UNKNOWN
        assert expired.retained_credential_ref is None

    asyncio.run(run())


def test_supervisor_fails_closed_with_direct_mode_escape(tmp_path: Path) -> None:
    class FailedInstaller:
        def resolve_engine_path(self):
            return None

        def status(self):
            return {
                "installed": False,
                "version": None,
                "reason": "model_hub_engine_archive_checksum_mismatch",
            }

        def contract_manifest(self):
            return {
                "name": "cliproxyapi",
                "version": "v7.2.95",
                "source_sha": "f" * 40,
                "assets": [],
            }

    supervisor = EngineSupervisor(
        installer=FailedInstaller(),
        state_store=EngineStateStore(tmp_path / "state"),
    )

    with pytest.raises(EngineUnavailableError) as exc_info:
        supervisor.ensure_running()
    assert exc_info.value.error_key == "models.engine.install_failed"
    assert exc_info.value.reason == "model_hub_engine_archive_checksum_mismatch"
    assert exc_info.value.direct_mode_available is True
