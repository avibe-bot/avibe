"""Real pinned-engine evidence for the Avibe v1 Chat -> Gemini fallback.

The existing Google matrix proves a one-response text path, not a tool-result
round trip or the exact finish/usage fields a Chat consumer must interpret.
Only the Gemini upstream is a fixture; router, gateway, adapter and CPA are real.
This is a characterization of CPA c404af96, including its lossy behavior, not
an assertion that it is a lossless substitute for a native Google adapter.
It extends MH-VIBEY-001/002 with Google-primary/Chat-ingress evidence, without
claiming coverage of their CLI-overlay or cross-hop-failover cases.
"""

from __future__ import annotations

import base64
import copy
import json
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
import pytest

from core.handlers.model_hub.provenance import SERVED_HOP_HEADER
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_vibey_consumer import _vibey_service
from tests.test_model_hub_google import _server, _sse
from tests.test_model_hub_l3 import _source


pytestmark = pytest.mark.e2e_model_hub

GEMINI_USAGE = {
    "promptTokenCount": 12,
    "cachedContentTokenCount": 4,
    "candidatesTokenCount": 3,
    "thoughtsTokenCount": 2,
    "totalTokenCount": 17,
}
CHAT_USAGE = {
    "prompt_tokens": 12,
    "completion_tokens": 5,
    "total_tokens": 17,
    "prompt_tokens_details": {"cached_tokens": 4},
    "completion_tokens_details": {"reasoning_tokens": 2},
}
# Synthetic opaque state, not a vendor secret. The field-2/field-1 envelope
# follows CPA (MIT), internal/signature/gemini_validation.go at c404af96.
_SIGNATURE_PAYLOAD = b"\x01" + b"synthetic-tool-state"
_SIGNATURE_CONTAINER = b"\x0a" + bytes([len(_SIGNATURE_PAYLOAD)]) + _SIGNATURE_PAYLOAD
THOUGHT_SIGNATURE = base64.b64encode(
    b"\x12" + bytes([len(_SIGNATURE_CONTAINER)]) + _SIGNATURE_CONTAINER,
).decode()
TOOL_ARGS = {"city": "上海"}
TOOL_RESULT = '{"temperature":21,"city":"上海"}'
TOOLS = [{
    "type": "function",
    "function": {
        "name": "weather",
        "description": "Return the fixture weather.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]


def _gemini_reply(parts, finish="STOP", *, usage=True):
    result = {
        "modelVersion": "gemini-3-pro-preview",
        "candidates": [{
            "index": 0, "content": {"role": "model", "parts": parts},
            "finishReason": finish,
        }],
    }
    if usage:
        result["usageMetadata"] = copy.deepcopy(GEMINI_USAGE)
    return result


@asynccontextmanager
async def _chat_gateway(tmp_path, monkeypatch, upstream, *, model="gemini-3-pro-preview"):
    with _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        async with _server(upstream) as root:
            source = _source("src_google001", "Gemini fixture", vendor="custom", protocol="google", model_id=model)
            source.base_url = root
            source.credential_ref = adapter.state_store.store_api_key(
                "synthetic-google-chat-key", vendor="custom", protocol="google", base_url=root,
            )
            config = _vibey_service(tmp_path, [source]).store.config
            service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            gateway = ModelHubTurnGateway(service)
            router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
            try:
                hop = await router.resolve_hop(
                    "menu-alias", process_scope="vibey:chat-gemini", turn_id="chat-gemini",
                )
                assert hop["protocol"] == "google"
                assert hop["base_url"].endswith("/vibey/v1beta")
                # The frontend is a caller choice; do not append Chat's route
                # to the native /v1beta base returned for the Google primary.
                endpoint = hop["base_url"].rsplit("/", 1)[0] + "/v1/chat/completions"
                origin = {"provider": "custom", "api": "google", "model": model}
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as client:
                    async def post(messages, *, stream, **options):
                        async with client.post(endpoint, headers={
                            "Authorization": f'Bearer {hop["token"]}', **hop["request_headers"],
                        }, json={
                            "model": hop["runtime_model"], "stream": stream,
                            "messages": messages, "max_tokens": 128, **options,
                        }) as response:
                            assert response.status == 200, await response.text()
                            # Observed before the consumer reads any model body.
                            assert json.loads(response.headers[SERVED_HOP_HEADER]) == origin
                            raw = await response.read()
                        if not stream:
                            return [json.loads(raw)]
                        data = [line[5:].strip() for line in raw.splitlines() if line.startswith(b"data:")]
                        assert data[-1] == b"[DONE]", raw
                        return [json.loads(frame) for frame in data if frame != b"[DONE]"]

                    yield post
                completion = router.settle_turn(
                    "chat-gemini", settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-03T03:00:00Z",
                )
                if completion:
                    await completion
                record = service.provenance.get("chat-gemini")
                assert record["outcome"] == "served", record
                assert record["served"]["origin"] == origin
                assert record["failed_attempts"] == []
            finally:
                await gateway.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("finish", "buffered_reason", "stream_reason"), [
    ("STOP", "stop", "stop"),
    ("MAX_TOKENS", "max_tokens", "max_tokens"),
    ("SAFETY", "safety", "stop"),
])
async def test_chat_google_text_finish_reason_and_usage(
    tmp_path, monkeypatch, stream, finish, buffered_reason, stream_reason,
):
    """Record native_finish_reason too: CPA's public length/safety values are not standard Chat."""
    requests = []

    async def upstream(request):
        requests.append((request.path, await request.json()))
        reply = _gemini_reply([{"text": "Fixture answer: 上海"}] if finish != "SAFETY" else [], finish)
        return web.Response(
            body=_sse(reply) if stream else json.dumps(reply).encode(),
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _chat_gateway(tmp_path, monkeypatch, upstream) as post:
        frames = await post([{"role": "user", "content": "Fixture question: 上海"}], stream=stream)
        assert len(requests) == 1
        path, body = requests[0]
        action = "streamGenerateContent" if stream else "generateContent"
        assert path == f"/v1beta/models/gemini-3-pro-preview:{action}"
        assert body["contents"] == [{"role": "user", "parts": [{"text": "Fixture question: 上海"}]}]
        assert body["generationConfig"]["maxOutputTokens"] == 128
        assert frames[-1]["usage"] == CHAT_USAGE
        choice = frames[-1]["choices"][0]
        message = choice["delta" if stream else "message"]
        assert message["content"] == ("Fixture answer: 上海" if finish != "SAFETY" else None)
        assert choice["native_finish_reason"] == finish.lower()
        assert choice["finish_reason"] == (stream_reason if stream else buffered_reason), frames


@pytest.mark.parametrize("stream", [False, True])
async def test_chat_google_colon_model_is_not_a_google_frontend_skip(tmp_path, monkeypatch, stream):
    """The colon restriction is on CPA's inbound Google action parser, not this executor."""
    requests = []

    async def upstream(request):
        requests.append((request.path, await request.json()))
        reply = _gemini_reply([{"text": "Colon model served"}])
        return web.Response(
            body=_sse(reply) if stream else json.dumps(reply).encode(),
            content_type="text/event-stream" if stream else "application/json",
        )

    async with _chat_gateway(tmp_path, monkeypatch, upstream, model="fixture:free") as post:
        frames = await post([{"role": "user", "content": "ping"}], stream=stream)
        assert len(requests) == 1
        action = "streamGenerateContent" if stream else "generateContent"
        assert requests[0][0] == f"/v1beta/models/fixture:free:{action}"
        assert requests[0][1]["model"] == "fixture:free"
        assert frames[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("supply_signature", [False, True])
async def test_chat_google_tool_round_trip_loses_signature_but_explicit_carrier_survives(
    tmp_path, monkeypatch, stream, supply_signature,
):
    """Replay the actual first response, not a fake that supplies CPA's missing fields.

    The separate input-carrier control complements MH-VIBEY-005: it proves
    whether the Hub strips a signature when the frontend differs but the
    provider/api/model origin is unchanged, without claiming a failover test.
    """
    requests = []
    function_part = {"functionCall": {"name": "weather", "args": TOOL_ARGS}, "thoughtSignature": THOUGHT_SIGNATURE}

    async def upstream(request):
        body = await request.json()
        requests.append(body)
        reply = _gemini_reply([function_part] if len(requests) == 1 else [{"text": "Weather: 21"}])
        if stream:
            content = copy.deepcopy(reply)
            content.pop("usageMetadata")
            content["candidates"][0].pop("finishReason")
            terminal = _gemini_reply([])
            raw = _sse(content) + _sse(terminal)
        else:
            raw = json.dumps(reply).encode()
        return web.Response(body=raw, content_type="text/event-stream" if stream else "application/json")

    async with _chat_gateway(tmp_path, monkeypatch, upstream) as post:
        messages = [{"role": "user", "content": "Weather in 上海?"}]
        frames = await post(messages, stream=stream, tools=TOOLS)
        call_message = next(
            choice["delta" if stream else "message"]
            for frame in frames for choice in frame["choices"]
            if choice["delta" if stream else "message"].get("tool_calls")
        )
        tool = copy.deepcopy(call_message["tool_calls"][0])
        tool.pop("index", None)
        # The real output lacks the signature. The input-carrier control below
        # is deliberately separate and is NOT evidence of round-trip retention.
        assert THOUGHT_SIGNATURE not in json.dumps(frames)
        assert "extra_content" not in tool
        assert tool["type"] == "function"
        assert tool["id"]
        assert tool["function"]["name"] == "weather"
        assert json.loads(tool["function"]["arguments"]) == TOOL_ARGS
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert frames[-1]["usage"] == CHAT_USAGE
        if supply_signature:
            tool["extra_content"] = {"google": {"thought_signature": THOUGHT_SIGNATURE}}
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [tool]},
            {"role": "tool", "tool_call_id": tool["id"], "content": TOOL_RESULT},
        ])
        final = await post(messages, stream=stream, tools=TOOLS)
        assert len(requests) == 2
        for body in requests:
            assert body["tools"] == [{"functionDeclarations": [{
                "name": "weather",
                "description": "Return the fixture weather.",
                "parametersJsonSchema": TOOLS[0]["function"]["parameters"],
            }]}]
        assert "Weather: 21" in json.dumps(final)
        calls = [part for content in requests[1]["contents"] for part in content["parts"] if "functionCall" in part]
        results = [part for content in requests[1]["contents"] for part in content["parts"] if "functionResponse" in part]
        assert len(calls) == len(results) == 1
        assert calls[0]["functionCall"] == {"name": "weather", "args": TOOL_ARGS}
        assert results[0]["functionResponse"]["name"] == "weather"
        assert "thoughtSignature" not in results[0]
        # CPA wraps the tool content's JSON string encoding as result, instead
        # of converting the JSON text into functionResponse.response fields.
        assert json.loads(results[0]["functionResponse"]["response"]["result"]) == TOOL_RESULT, results
        assert calls[0]["thoughtSignature"] == (
            THOUGHT_SIGNATURE if supply_signature else "skip_thought_signature_validator"
        ), (tool, calls)


@pytest.mark.parametrize("ending", [
    "same_frame", "usage_with_candidate", "usage_only", "without_usage",
    "missing_finish", "missing_finish_and_usage",
])
async def test_chat_google_stream_terminal_boundaries(tmp_path, monkeypatch, ending):
    """Without traceId, split finish/usage is lost; CPA's [DONE] is not semantic proof."""
    requests = []
    content = _gemini_reply([{"text": "streamed answer"}], usage=False)
    content["candidates"][0].pop("finishReason")
    finish = _gemini_reply([], "MAX_TOKENS", usage=False)
    tails = {
        "same_frame": [_gemini_reply([], "MAX_TOKENS")],
        "usage_with_candidate": [finish, {"candidates": [{"index": 0}], "usageMetadata": GEMINI_USAGE}],
        "usage_only": [finish, {"usageMetadata": GEMINI_USAGE}],
        "without_usage": [finish],
        "missing_finish": [{"usageMetadata": GEMINI_USAGE}],
        "missing_finish_and_usage": [],
    }

    async def upstream(request):
        requests.append((request.path, await request.json()))
        frames = [content] + tails[ending]
        return web.Response(body=b"".join(_sse(frame) for frame in frames), content_type="text/event-stream")

    async with _chat_gateway(tmp_path, monkeypatch, upstream) as post:
        frames = await post(
            [{"role": "user", "content": "Finish boundary fixture"}],
            stream=True, stream_options={"include_usage": True},
        )
        assert len(requests) == 1
        assert requests[0][0].endswith(":streamGenerateContent")
        reasons = [
            (choice.get("finish_reason"), choice.get("native_finish_reason"))
            for frame in frames for choice in frame["choices"]
            if choice.get("finish_reason") is not None
        ]
        # CPA filters usage on frames without their own finishReason, then the
        # Chat translator only emits a finish when both fields are available.
        expected = [("max_tokens", "max_tokens")] if ending == "same_frame" else []
        assert reasons == expected, frames
        usages = [frame["usage"] for frame in frames if "usage" in frame]
        assert usages == ([CHAT_USAGE] if ending == "same_frame" else []), frames
