"""MH-PROTOCOL-004: inspect request identity emitted by the real Codex CLI."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from config.v2_config import ModelHubBackendModelConfig
from modules.agents.codex.transport import CodexTransport
from modules.agents.model_hub import ModelHubLaunch, build_codex_hub_launch
from tests.e2e.drivers import mock_llm_upstream as upstream
from tests.e2e.test_model_hub_catalog_consumer import (
    codex_catalog_runtime,  # noqa: F401 -- fixture dependency
    rejected_external_proxy,  # noqa: F401 -- fixture dependency
)
from vibe.backend_model_catalog import _codex_hub_catalog_bytes


pytestmark = pytest.mark.e2e_model_hub
METADATA_HEADER = "x-codex-turn-metadata"
TOKEN = "isolated-request-metadata-fixture"
MODELS = ("路由甲-alias", "route-b-alias")


@pytest.fixture
def metadata_runtime(codex_catalog_runtime, monkeypatch, record_property):
    binary, runtime, raw_catalog = codex_catalog_runtime
    version = subprocess.run(
        [binary, "--version"], cwd=runtime.home, env=runtime.env,
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip()
    record_property("codex_version", version)
    # This is the HTTP receiver's capture allowlist, not an injected header.
    monkeypatch.setattr(
        upstream, "CAPTURED_HEADER_NAMES",
        (*upstream.CAPTURED_HEADER_NAMES, METADATA_HEADER),
    )
    Path(runtime.env["CODEX_HOME"], "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
        "model_auto_compact_token_limit = 100000\n",
    )
    catalog_path = runtime.home / "metadata-models.json"
    catalog_path.write_bytes(_codex_hub_catalog_bytes(
        raw_catalog,
        [ModelHubBackendModelConfig(id=model, context_window=128_000).to_payload() for model in MODELS],
    ))
    return binary, runtime, catalog_path


@asynccontextmanager
async def _transport(fixture, gateway, *, overrides=(), launch=None):
    """Run one native process; a live gateway test may supply its issued launch."""
    binary, runtime, catalog_path = fixture
    if launch is None:
        launch = ModelHubLaunch(
            backend="codex", channel="hub", requested_model=MODELS[0],
            runtime_model=MODELS[0], target_model="unrelated-target",
            gateway_base_url=gateway.url, gateway_token=TOKEN,
        )
    args, env = build_codex_hub_launch(
        list(overrides), runtime.env, launch, model_catalog_path=catalog_path,
    )
    transport = CodexTransport(
        binary=binary, cwd=str(runtime.home), runtime_args=args, runtime_env=env,
    )
    notifications = []
    completed = asyncio.Queue()

    async def notify(method, params):
        notifications.append((method, params))
        if method == "turn/completed":
            await completed.put(params)

    transport.on_notification(notify)
    try:
        await transport.start()
        yield transport, completed, notifications
    finally:
        await transport.stop()


async def _thread(transport, model, **extra):
    response = await transport.send_request(
        "thread/start",
        {"model": model, "cwd": transport._cwd, "approvalPolicy": "never", **extra},
    )
    return response["thread"]["id"]


async def _turn(transport, thread_id, identity):
    return await transport.send_request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "请验证本轮路由，保留中文与 café。"}],
            "responsesapiClientMetadata": identity,
        },
    )


async def _completed(queue):
    result = await asyncio.wait_for(queue.get(), timeout=30)
    assert result["turn"]["status"] == "completed", result
    return result


def _assert_identity(request, identity, *, body_metadata=True):
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    header = json.loads(request["headers"][METADATA_HEADER])
    assert {key: header[key] for key in identity} == identity
    body = request["body"]
    if body_metadata:
        metadata = json.loads(body["client_metadata"][METADATA_HEADER])
        assert {key: metadata[key] for key in identity} == identity
        assert metadata["turn_id"] == header["turn_id"]
    else:
        assert "client_metadata" not in body


@pytest.fixture
def large_initial_usage(monkeypatch):
    """Trigger automatic compaction with a deterministic upstream token count."""
    original_frames = upstream._responses_stream_frames
    calls = 0

    def frames(model):
        nonlocal calls
        calls += 1
        result = original_frames(model)
        if calls == 1:
            completed = json.loads(result[-1].split(b"data: ", 1)[1])
            completed["response"]["usage"].update(input_tokens=150_000, total_tokens=150_005)
            result[-1] = upstream._sse("response.completed", completed)
        return result

    monkeypatch.setattr(upstream, "_responses_stream_frames", frames)


def test_codex_auto_compaction_wire_metadata(metadata_runtime, large_initial_usage):
    """MH-PROTOCOL-004: real Hub local compaction retains the new turn identity."""
    identities = [
        {"avibe_route_id": "route-opaque-1", "avibe_turn_id": f"avibe-turn-{number}"}
        for number in (1, 2)
    ]

    async def probe(gateway):
        async with _transport(metadata_runtime, gateway) as (transport, queue, notifications):
            process = transport._process
            thread_id = await _thread(transport, MODELS[0])
            for identity in identities:
                await _turn(transport, thread_id, identity)
                await _completed(queue)
            assert transport._process is process and process.returncode is None
            assert any(
                method == "item/completed" and params.get("item", {}).get("type") == "contextCompaction"
                for method, params in notifications
            ), notifications

    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        asyncio.run(probe(gateway))
        requests = gateway.requests()
    assert len(requests) == 3, requests
    assert [request["path"] for request in requests] == ["/v1/responses"] * 3
    for index, request in enumerate(requests):
        _assert_identity(request, identities[min(index, 1)])
        assert request["body"]["model"] == MODELS[0]
    assert "中文" in json.dumps(requests[0]["body"]["input"], ensure_ascii=False)


def test_codex_remote_compact_request_uses_header_metadata(
    metadata_runtime, large_initial_usage, monkeypatch,
):
    """MH-PROTOCOL-004: inspect remote compact's carrier, not gateway support.

    The Hub provider uses local compaction. A test-only OpenAI provider name
    with remote_compaction_v2 disabled activates the legacy remote endpoint.
    Its HTTP header survives even though its body has no client_metadata. The
    receiver rejects that endpoint deterministically; no fake compaction
    output or gateway support is implied.
    """
    original_post = upstream.MockLLMUpstreamHandler.do_POST

    def post(handler):
        if handler.path != "/v1/responses/compact":
            return original_post(handler)
        handler._capture(handler.path, handler._read_json_body())
        handler._write_json(400, {"error": {
            "type": "invalid_request_error", "message": "synthetic compact probe stop",
        }})

    monkeypatch.setattr(upstream.MockLLMUpstreamHandler, "do_POST", post)
    first = {"avibe_route_id": "route-compact", "avibe_turn_id": "avibe-turn-before-compact"}
    second = {**first, "avibe_turn_id": "avibe-turn-compact"}

    async def probe(gateway):
        overrides = [
            "-c", 'model_providers.avibe_model_hub.name="OpenAI"',
            "-c", "features.remote_compaction_v2=false",
        ]
        async with _transport(metadata_runtime, gateway, overrides=overrides) as (transport, queue, _notifications):
            thread_id = await _thread(transport, MODELS[0])
            await _turn(transport, thread_id, first)
            await _completed(queue)
            await _turn(transport, thread_id, second)
            result = await asyncio.wait_for(queue.get(), timeout=30)
            assert result["turn"]["status"] == "failed", result
            assert "synthetic compact probe stop" in result["turn"]["error"]["message"]

    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        asyncio.run(probe(gateway))
        requests = gateway.requests()
    assert [request["path"] for request in requests] == ["/v1/responses", "/v1/responses/compact"]
    _assert_identity(requests[0], first)
    _assert_identity(requests[1], second, body_metadata=False)
    assert requests[1]["body"]["model"] == MODELS[0]


def test_codex_concurrent_threads_keep_distinct_metadata(metadata_runtime, monkeypatch):
    """MH-CODEX-METADATA-001: live concurrent requests share a process and auth."""
    barrier = threading.Barrier(2, timeout=10)
    original_stream = upstream.MockLLMUpstreamHandler._write_stream

    def stream(handler, protocol, body, behavior):
        # Neither response can finish before both requests arrive at HTTP.
        barrier.wait()
        return original_stream(handler, protocol, body, behavior)

    monkeypatch.setattr(upstream.MockLLMUpstreamHandler, "_write_stream", stream)
    identities = {
        model: {"avibe_route_id": f"route-{index}", "avibe_turn_id": f"avibe-turn-{index}"}
        for index, model in enumerate(MODELS)
    }

    async def probe(gateway):
        async with _transport(metadata_runtime, gateway) as (transport, queue, _notifications):
            process = transport._process
            threads = await asyncio.gather(*(_thread(transport, model) for model in MODELS))
            assert len(set(threads)) == 2
            await asyncio.gather(*(
                _turn(transport, thread_id, identities[model])
                for thread_id, model in zip(threads, MODELS)
            ))
            finished = [await _completed(queue), await _completed(queue)]
            assert {result["threadId"] for result in finished} == set(threads)
            assert transport._process is process and process.returncode is None

    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        asyncio.run(probe(gateway))
        requests = gateway.requests()
    assert len(requests) == 2
    assert {request["body"]["model"] for request in requests} == set(MODELS)
    for request in requests:
        _assert_identity(request, identities[request["body"]["model"]])
    assert len({json.loads(request["headers"][METADATA_HEADER])["turn_id"] for request in requests}) == 2


def test_codex_tool_continuation_keeps_metadata(metadata_runtime, monkeypatch):
    """MH-CODEX-METADATA-001: inference retains the tool caller's identity."""
    original_frames = upstream._responses_stream_frames
    calls = 0
    tool_calls = []

    def frames(model):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original_frames(model)
        tool = {
            "type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": "read_fixture", "arguments": '{"key":"中文"}', "status": "completed",
        }
        completed = json.loads(original_frames(model)[-1].split(b"data: ", 1)[1])
        completed["response"]["output"] = [tool]
        return [
            original_frames(model)[0],
            upstream._sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0,
                "item": {**tool, "status": "in_progress", "arguments": ""},
            }),
            upstream._sse("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0, "item": tool,
            }),
            upstream._sse("response.completed", completed),
        ]

    monkeypatch.setattr(upstream, "_responses_stream_frames", frames)
    identity = {"avibe_route_id": "route-tool", "avibe_turn_id": "avibe-turn-tool"}

    async def probe(gateway):
        async with _transport(metadata_runtime, gateway) as (transport, queue, _notifications):
            async def tool_result(_request_id, method, params):
                assert method == "item/tool/call", (method, params)
                tool_calls.append(params)
                return {"contentItems": [{"type": "inputText", "text": "fixture-output-中文"}], "success": True}

            transport.on_server_request(tool_result)
            thread_id = await _thread(transport, MODELS[0], dynamicTools=[{
                "name": "read_fixture", "description": "Read an in-memory test fixture.",
                "inputSchema": {
                    "type": "object", "properties": {"key": {"type": "string"}},
                    "required": ["key"], "additionalProperties": False,
                },
            }])
            await _turn(transport, thread_id, identity)
            await _completed(queue)

    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        asyncio.run(probe(gateway))
        requests = gateway.requests()
    assert len(tool_calls) == 1, requests
    assert tool_calls[0]["arguments"] == {"key": "中文"}
    assert len(requests) == 2
    for request in requests:
        _assert_identity(request, identity)
    outputs = [item for item in requests[1]["body"]["input"] if item.get("type") == "function_call_output"]
    assert len(outputs) == 1 and outputs[0]["call_id"] == "call_fixture"
    assert "fixture-output-中文" in json.dumps(outputs[0], ensure_ascii=False)


@pytest.mark.parametrize("failure", ["http_503", "interrupted_stream"])
def test_codex_transport_retry_keeps_metadata(metadata_runtime, monkeypatch, failure):
    """MH-CODEX-METADATA-001: native retries reuse the original identity and auth."""
    original_stream = upstream.MockLLMUpstreamHandler._write_stream
    attempts = 0

    def stream(handler, protocol, body, behavior):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "http_503":
                return handler._write_json(503, {"error": {"message": "synthetic retry", "type": "server_error"}})
            return original_stream(handler, protocol, body, "interrupt_after_first_output")
        return original_stream(handler, protocol, body, behavior)

    monkeypatch.setattr(upstream.MockLLMUpstreamHandler, "_write_stream", stream)
    identity = {"avibe_route_id": "route-retry", "avibe_turn_id": "avibe-turn-retry"}

    async def probe(gateway):
        # Hub disables HTTP retries; override only in the HTTP capability probe.
        overrides = ["-c", "model_providers.avibe_model_hub.request_max_retries=1"] if failure == "http_503" else []
        async with _transport(metadata_runtime, gateway, overrides=overrides) as (transport, queue, _notifications):
            thread_id = await _thread(transport, MODELS[0])
            await _turn(transport, thread_id, identity)
            await _completed(queue)

    with upstream.MockLLMUpstream() as gateway:
        gateway.configure(protocol="openai_responses")
        asyncio.run(probe(gateway))
        requests = gateway.requests()
    assert len(requests) == attempts == 2
    for request in requests:
        _assert_identity(request, identity)
    assert len({json.loads(request["headers"][METADATA_HEADER])["turn_id"] for request in requests}) == 1
