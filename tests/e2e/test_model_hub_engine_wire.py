"""Stream and failure wire behavior Vibey receives through the gateway and the pinned CPA.

Each case drives the real gateway, router, adapter and engine against a
loopback upstream that misbehaves in one specific way. The assertions pin what
Avibe relies on; ``tmp_path / "evidence.json"`` records the full observation,
including Model Hub's own outcome for the Turn, so two engines can be compared.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import time
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import quote

import aiohttp
from aiohttp import web
import pytest

from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.e2e.test_model_hub_runtime import _isolated_engine_adapter
from tests.scenario_harness.model_hub import MemoryModelHubStore, service_for
from tests.test_model_hub_google import _server, _sse
from tests.test_model_hub_l3 import _source
from tests.test_model_hub_vibey_consumer import _vibey_service
from vibe.model_hub_runtime import supervisor as engine_supervisor


pytestmark = pytest.mark.e2e_model_hub

MODEL = "fixture-model"
PROTOCOLS = ("openai_chat", "openai_responses", "anthropic", "google")
_FRONTEND_PATH = {
    "anthropic": "/v1/messages",
    "openai_responses": "/v1/responses",
    "openai_chat": "/v1/chat/completions",
}
_TURN = "wire"


def _partial_frames(protocol: str) -> list[bytes]:
    """Output that a healthy stream would continue; no terminal frame."""
    if protocol == "openai_chat":
        return [_sse({"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1700000000,
                      "model": MODEL, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "partial"},
                                                   "finish_reason": None}]})]
    if protocol == "openai_responses":
        return [
            b"event: response.created\n" + _sse({"type": "response.created", "sequence_number": 0, "response": {
                "id": "resp_fixture", "object": "response", "status": "in_progress", "model": MODEL, "output": []}}),
            b"event: response.output_item.added\n" + _sse({
                "type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                "item": {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "in_progress",
                         "content": []}}),
            b"event: response.output_text.delta\n" + _sse({
                "type": "response.output_text.delta", "sequence_number": 2, "item_id": "msg_fixture",
                "output_index": 0, "content_index": 0, "delta": "partial"}),
        ]
    if protocol == "anthropic":
        return [
            b"event: message_start\n" + _sse({"type": "message_start", "message": {
                "id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL, "content": [],
                "stop_reason": None, "usage": {"input_tokens": 3, "output_tokens": 0}}}),
            b"event: content_block_start\n" + _sse({"type": "content_block_start", "index": 0,
                                                    "content_block": {"type": "text", "text": ""}}),
            b"event: content_block_delta\n" + _sse({"type": "content_block_delta", "index": 0,
                                                    "delta": {"type": "text_delta", "text": "partial"}}),
        ]
    return [_sse({"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": "partial"}]}}],
                  "modelVersion": MODEL})]


_CHAT_FINISH = _sse({"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1700000000,
                     "model": MODEL, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
_CHAT_USAGE = _sse({"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 1700000000,
                    "model": MODEL, "choices": [],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})
# OpenAI's reasoning ciphertext is Fernet-shaped; xAI's carries no envelope, only opaque base64.
_ENCRYPTED = "g" + "A" * 97
_XAI_ENCRYPTED = base64.b64encode(bytes(range(7, 199))).decode()


def _responses_reasoning_tool_frames(encrypted: str = _ENCRYPTED) -> list[bytes]:
    events = [
        {"type": "response.created", "response": {"id": "resp_rt", "object": "response", "status": "in_progress",
                                                  "model": MODEL, "output": []}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning", "summary": []}},
        {"type": "response.reasoning_summary_part.added", "item_id": "rs_1", "output_index": 0,
         "summary_index": 0, "part": {"type": "summary_text", "text": ""}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "output_index": 0,
         "summary_index": 0, "delta": "Checking the "},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "output_index": 0,
         "summary_index": 0, "delta": "weather tool."},
        {"type": "response.reasoning_summary_text.done", "item_id": "rs_1", "output_index": 0,
         "summary_index": 0, "text": "Checking the weather tool."},
        {"type": "response.reasoning_summary_part.done", "item_id": "rs_1", "output_index": 0,
         "summary_index": 0, "part": {"type": "summary_text", "text": "Checking the weather tool."}},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"id": "rs_1", "type": "reasoning", "encrypted_content": encrypted,
                  "summary": [{"type": "summary_text", "text": "Checking the weather tool."}]}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"id": "fc_1", "type": "function_call", "status": "in_progress", "call_id": "call_fixture",
                  "name": "weather", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1,
         "delta": "{\"city\":"},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 1,
         "delta": "\"上海\"}"},
        {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 1,
         "arguments": "{\"city\":\"上海\"}"},
        {"type": "response.output_item.done", "output_index": 1,
         "item": {"id": "fc_1", "type": "function_call", "status": "completed", "call_id": "call_fixture",
                  "name": "weather", "arguments": "{\"city\":\"上海\"}"}},
        {"type": "response.completed", "response": {
            "id": "resp_rt", "object": "response", "status": "completed", "model": MODEL, "output": [],
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}}},
    ]
    return [
        f"event: {event['type']}\n".encode() + _sse({**event, "sequence_number": index})
        for index, event in enumerate(events)
    ]


def _frontend_request(protocol: str, hop: dict, *, stream: bool, extra: dict | None = None) -> tuple[str, dict]:
    base = hop["base_url"].rsplit("/", 1)[0]
    prompt = "wire fixture"
    if protocol == "google":
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return (f'{base}/v1beta/models/{quote(hop["runtime_model"], safe="")}:{method}',
                {"contents": [{"role": "user", "parts": [{"text": prompt}]}], **(extra or {})})
    body = {"model": hop["runtime_model"], "stream": stream}
    if protocol == "openai_responses":
        body.update(input=prompt, max_output_tokens=64)
    else:
        body.update(messages=[{"role": "user", "content": prompt}], max_tokens=64)
    body.update(extra or {})
    return base + _FRONTEND_PATH[protocol], body


class _Harness:
    def __init__(self, service, router, adapter, hop, requests) -> None:
        self.service = service
        self.router = router
        self.adapter = adapter
        self.hop = hop
        # Requests per Source, in route order.
        self.requests = requests

    async def outcome(self) -> dict:
        """Model Hub's own verdict on the Turn, and the state it left on each Source."""
        completion = self.router.settle_turn(_TURN, settled_by=SETTLED_BY_TERMINAL_RESULT, ts="2026-10-11T09:00:00Z")
        if completion:
            await completion
        record = self.service.provenance.get(_TURN) or {}
        served = record.get("served") or {}
        return {
            "provenance_outcome": record.get("outcome"),
            "served_source": served.get("source_id"),
            "failed_attempts": [
                {key: attempt.get(key) for key in ("source_id", "reason", "http_status", "stream_started")}
                for attempt in record.get("failed_attempts") or []
            ],
            "terminal_error": {key: (record.get("terminal_error") or {}).get(key)
                               for key in ("source_id", "reason", "http_status", "stream_started")},
            "sources": {source.id: [source.state.status, source.state.detail_key]
                        for source in self.service.store.load().sources},
        }


@asynccontextmanager
async def _through_engine(
    tmp_path, monkeypatch, protocol: str, *handlers, model_id: str = MODEL, efforts=(), vendor: str = "custom",
):
    """A route of one Source per handler, in order, all speaking ``protocol`` and serving ``model_id``."""
    requests: list[list[dict]] = [[] for _ in handlers]

    def recording(index, handler):
        async def respond(request):
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 - the fixture records whatever arrived
                body = None
            requests[index].append({"path": request.path, "body": body, "at": time.monotonic()})
            return await handler(request, body)

        return respond

    with _isolated_engine_adapter(tmp_path, monkeypatch) as adapter:
        async with AsyncExitStack() as stack:
            sources = []
            for index, handler in enumerate(handlers):
                root = await stack.enter_async_context(_server(recording(index, handler)))
                source = _source(f"src_wire{index:04d}", f"Wire fixture {index}", vendor=vendor,
                                 protocol=protocol, model_id=model_id)
                source.models[0].reasoning_efforts = list(efforts)
                source.base_url = root + ("/v1" if protocol == "openai_responses" else "")
                source.credential_ref = adapter.state_store.store_api_key(
                    f"sk-synthetic-wire-{index}", vendor=vendor, protocol=protocol, base_url=source.base_url,
                )
                sources.append(source)
            config = _vibey_service(tmp_path, sources).store.config
            service = service_for(tmp_path, MemoryModelHubStore(config), adapter)
            await service.runtime_start()
            gateway = ModelHubTurnGateway(service)
            router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
            try:
                hop = await router.resolve_hop("menu-alias", process_scope="vibey:wire", turn_id=_TURN)
                yield _Harness(service, router, adapter, hop, requests)
            finally:
                await gateway.close()


async def _post(url: str, hop: dict, body: dict, *, timeout: float = 150) -> dict:
    started = time.monotonic()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as client:
        headers = {"Authorization": f'Bearer {hop["token"]}', **hop["request_headers"]}
        async with client.post(url, headers=headers, json=body) as response:
            transport_error = None
            try:
                raw = await response.read()
            except aiohttp.ClientPayloadError as exc:
                raw = b""
                transport_error = type(exc).__name__
            return {
                "status": response.status,
                "content_type": response.headers.get("Content-Type", "").split(";")[0],
                "raw": raw.decode("utf-8", errors="replace"),
                "transport_error": transport_error,
                "seconds": round(time.monotonic() - started, 3),
            }


def _frames(raw: str) -> list[str]:
    return [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]


def _json_frames(raw: str) -> list[dict]:
    parsed = []
    for frame in _frames(raw):
        try:
            parsed.append(json.loads(frame))
        except json.JSONDecodeError:
            continue
    return parsed


def _record(tmp_path, payload) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    (tmp_path / "evidence.json").write_text(text, encoding="utf-8")
    # One line per case, so a run's captured output compares across engines.
    print(f"EVIDENCE {os.environ.get('PYTEST_CURRENT_TEST', '').split(' ')[0]} {text}")


async def _stream(request, frames: list[bytes], *, close: str = "eof") -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for frame in frames:
        await response.write(frame)
    if close == "abort":
        request.transport.close()
    else:
        await response.write_eof()
    return response


def _upstream_error(status: int, body_kind: str):
    async def handler(request, body):
        if body_kind == "text":
            return web.Response(status=status, text="upstream timeout\n", content_type="text/plain")
        if body_kind == "html":
            return web.Response(status=status, text=f"<html>{status}</html>", content_type="text/html")
        return web.json_response({"error": {"message": "fixture timeout", "type": "server_error",
                                            "code": "upstream_timeout"}}, status=status)

    return handler


async def _healthy(protocol: str):
    async def handler(request, body):
        frames = list(_partial_frames(protocol))
        if protocol == "openai_chat":
            frames += [_CHAT_FINISH, _CHAT_USAGE]
        elif protocol == "openai_responses":
            frames.append(b"event: response.completed\n" + _sse({"type": "response.completed", "sequence_number": 3,
                          "response": {"id": "resp_fixture", "object": "response", "status": "completed",
                                       "model": MODEL, "output": [],
                                       "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}}))
        elif protocol == "anthropic":
            frames += [b"event: content_block_stop\n" + _sse({"type": "content_block_stop", "index": 0}),
                       b"event: message_delta\n" + _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                                         "usage": {"output_tokens": 1}}),
                       b"event: message_stop\n" + _sse({"type": "message_stop"})]
        else:
            frames.append(_sse({"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": "."}]},
                                                "finishReason": "STOP"}], "modelVersion": MODEL,
                                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1,
                                                  "totalTokenCount": 4}}))
        return await _stream(request, frames)

    return handler


@pytest.mark.parametrize(("protocol", "status", "body_kind"), [
    *[(protocol, 408, "text") for protocol in PROTOCOLS],
    ("openai_chat", 408, "json"),
    ("openai_chat", 502, "html"),
])
async def test_upstream_http_error_on_a_single_source_route(tmp_path, monkeypatch, protocol, status, body_kind) -> None:
    """What the consumer gets, and when, after a pre-stream upstream error on the only Source."""
    async with _through_engine(tmp_path, monkeypatch, protocol, _upstream_error(status, body_kind)) as harness:
        url, body = _frontend_request(protocol, harness.hop, stream=True)
        observed = await _post(url, harness.hop, body)
        outcome = await harness.outcome()
    calls = harness.requests[0]
    _record(tmp_path, {"protocol": protocol, "upstream": [status, body_kind], "observed": observed,
                       "outcome": outcome, "upstream_calls": len(calls),
                       "call_offsets": [round(call["at"] - calls[0]["at"], 1) for call in calls]})
    assert observed["status"] >= 400, observed
    # The engine runs with zero retries: every upstream call is one Model Hub attempt.
    assert len(calls) == max(1, len(outcome["failed_attempts"])), (calls, outcome)
    assert outcome["provenance_outcome"] != "served", outcome


async def test_pre_stream_408_with_a_second_hop(tmp_path, monkeypatch) -> None:
    """A plain-text 408 from the first hop either surfaces or moves the Turn to the next hop."""
    protocol = "openai_chat"
    async with _through_engine(tmp_path, monkeypatch, protocol, _upstream_error(408, "text"),
                               await _healthy(protocol)) as harness:
        url, body = _frontend_request(protocol, harness.hop, stream=True)
        observed = await _post(url, harness.hop, body)
        outcome = await harness.outcome()
    _record(tmp_path, {"observed": observed, "outcome": outcome,
                       "upstream_calls": [len(calls) for calls in harness.requests]})
    # v7.3.16 typed a text 408 as invalid_request_error, so Avibe surfaced a
    # 400 and never tried the next hop. v8 types it server_error.
    assert observed["status"] == 200, observed
    assert outcome["served_source"] == "src_wire0001", outcome
    assert [len(calls) for calls in harness.requests] == [1, 1]


async def test_responses_stream_with_zero_bytes_is_a_failed_turn(tmp_path, monkeypatch) -> None:
    async def handler(request, body):
        return await _stream(request, [])

    async with _through_engine(tmp_path, monkeypatch, "openai_responses", handler) as harness:
        url, body = _frontend_request("openai_responses", harness.hop, stream=True)
        observed = await _post(url, harness.hop, body)
        outcome = await harness.outcome()
    _record(tmp_path, {"observed": observed, "outcome": outcome})
    assert observed["status"] >= 400, observed
    assert outcome["provenance_outcome"] != "served", outcome


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_upstream_drop_mid_stream_is_not_a_clean_completion(tmp_path, monkeypatch, protocol) -> None:
    async def handler(request, body):
        return await _stream(request, _partial_frames(protocol), close="abort")

    async with _through_engine(tmp_path, monkeypatch, protocol, handler) as harness:
        url, body = _frontend_request(protocol, harness.hop, stream=True)
        observed = await _post(url, harness.hop, body)
        outcome = await harness.outcome()
    _record(tmp_path, {"protocol": protocol, "observed": observed, "tail": _frames(observed["raw"])[-3:],
                       "outcome": outcome})
    assert "partial" in observed["raw"]
    assert outcome["provenance_outcome"] != "served", outcome
    if protocol == "openai_responses":
        # The in-stream shape of an incomplete Codex Responses stream (the
        # astra-style drop) is what Vibey's retry policy keys on.
        terminal = _json_frames(observed["raw"])[-1]
        assert terminal == {
            "type": "error",
            "error": {
                "code": "request_timeout",
                "message": "stream error: stream disconnected before completion: "
                           "stream closed before response.completed",
                "param": None,
                "type": "server_error",
            },
            "sequence_number": 3,
        }, terminal


@pytest.mark.parametrize("ending", ["done_marker", "eof", "finish_then_eof"])
async def test_chat_stream_terminal_depends_on_finish_reason(tmp_path, monkeypatch, ending) -> None:
    """A Chat stream is complete only when a chunk carried ``finish_reason``.

    The second request, sent at once to the same single-Source route with a
    healthy upstream, shows what an incomplete stream costs the next Turn.
    """
    frames = list(_partial_frames("openai_chat"))
    if ending == "done_marker":
        frames.append(b"data: [DONE]\n\n")
    elif ending == "finish_then_eof":
        frames += [_CHAT_FINISH, _CHAT_USAGE]
    healthy = await _healthy("openai_chat")
    calls = 0

    async def handler(request, body):
        nonlocal calls
        calls += 1
        return await (_stream(request, frames) if calls == 1 else healthy(request, body))

    async with _through_engine(tmp_path, monkeypatch, "openai_chat", handler) as harness:
        url, body = _frontend_request("openai_chat", harness.hop, stream=True)
        observed = await _post(url, harness.hop, body)
        outcome = await harness.outcome()
        harness.hop = await harness.router.resolve_hop("menu-alias", process_scope="vibey:wire", turn_id=_TURN + "-2")
        url, body = _frontend_request("openai_chat", harness.hop, stream=True)
        following = await _post(url, harness.hop, body)
    tail = _frames(observed["raw"])
    _record(tmp_path, {"ending": ending, "observed": observed, "tail": tail[-3:], "outcome": outcome,
                       "next_request": {key: following[key] for key in ("status", "seconds", "transport_error")}
                       | {"tail": _frames(following["raw"])[-2:]}})
    if ending == "finish_then_eof":
        assert tail[-1] == "[DONE]", tail
        assert outcome["provenance_outcome"] == "served", outcome
    else:
        assert "[DONE]" not in tail, tail
        assert any("error" in frame for frame in _json_frames(observed["raw"])), tail
        assert outcome["provenance_outcome"] != "served", outcome
    assert following["status"] == 200, following


@pytest.mark.parametrize(("vendor", "encrypted"), [("custom", _ENCRYPTED), ("xai", _XAI_ENCRYPTED)],
                         ids=["fernet", "xai"])
async def test_responses_stream_with_reasoning_and_tool_call(tmp_path, monkeypatch, vendor, encrypted) -> None:
    """MH-HOSTED-GATEWAY-008: reasoning summaries and a tool call pass through in order; the replay keeps its
    items, and a reasoning item reaches the Source that issued it with its ciphertext and id, xAI's included."""
    async def handler(request, body):
        return await _stream(request, _responses_reasoning_tool_frames(encrypted))

    replay = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "weather in 上海?"}]},
        {"type": "reasoning", "id": "rs_1", "status": "completed", "encrypted_content": encrypted,
         "summary": [{"type": "summary_text", "text": "Checking the weather tool."}]},
        {"type": "function_call", "id": "fc_1", "status": "completed", "call_id": "call_fixture",
         "name": "weather", "arguments": "{\"city\":\"上海\"}"},
        {"type": "function_call_output", "call_id": "call_fixture", "output": "{\"temperature\":21}"},
    ]
    tools = [{"type": "function", "name": "weather", "description": "fixture",
              "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    async with _through_engine(tmp_path, monkeypatch, "openai_responses", handler, vendor=vendor) as harness:
        url, body = _frontend_request("openai_responses", harness.hop, stream=True, extra={
            "tools": tools, "reasoning": {"effort": "high", "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        })
        first = await _post(url, harness.hop, body)
        second = await _post(url, harness.hop, {**body, "input": replay})
        outcome = await harness.outcome()
    events = _json_frames(first["raw"])
    types = [event.get("type") for event in events]
    completed = next((event for event in events if event.get("type") == "response.completed"), {})
    replayed = harness.requests[0][-1]["body"]["input"]
    _record(tmp_path, {"first_status": first["status"], "event_types": types,
                       "completed_output": completed.get("response", {}).get("output"),
                       "second_status": second["status"], "replayed_input_upstream": replayed,
                       "outcome": outcome})
    assert first["status"] == 200, first
    assert types[0] == "response.created" and types[-1] == "response.completed", types
    assert types.count("response.reasoning_summary_text.delta") == 2, types
    assert types.count("response.function_call_arguments.delta") == 2, types
    assert types.index("response.reasoning_summary_text.done") < types.index("response.output_item.added", 2)
    assert [item["type"] for item in completed["response"]["output"]] == ["reasoning", "function_call"]
    assert second["status"] == 200, second
    reasoning, call = replayed[1], replayed[2]
    assert reasoning["id"] == "rs_1" and reasoning["encrypted_content"] == encrypted, reasoning
    assert call["call_id"] == "call_fixture" and call["arguments"] == "{\"city\":\"上海\"}", call


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_consumer_cancel_closes_upstream_and_keeps_the_source(tmp_path, monkeypatch, protocol) -> None:
    """A consumer that stops reading cancels the upstream call and leaves the Source routable.

    An initial Responses stream is held until its upstream terminal (one exact overload replay,
    docs/plans/openai-streamed-overload-retry.md), so its consumer sees no headers while the
    upstream is still writing: it gives up while waiting instead of after the first bytes.
    """
    upstream_closed: asyncio.Future = asyncio.get_running_loop().create_future()
    healthy = await _healthy(protocol)
    calls = 0

    async def handler(request, body):
        nonlocal calls
        calls += 1
        if calls > 1:
            return await healthy(request, body)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        frames = _partial_frames(protocol)
        try:
            for frame in frames:
                await response.write(frame)
            for _ in range(150):
                await asyncio.sleep(0.2)
                await response.write(frames[-1])
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            if not upstream_closed.done():
                upstream_closed.set_result(time.monotonic())
            raise
        return response

    async with _through_engine(tmp_path, monkeypatch, protocol, handler) as harness:
        url, body = _frontend_request(protocol, harness.hop, stream=True)
        headers = {"Authorization": f'Bearer {harness.hop["token"]}', **harness.hop["request_headers"]}
        async with aiohttp.ClientSession() as client:
            if protocol == "openai_responses":
                request = asyncio.ensure_future(client.post(url, headers=headers, json=body))
                done, _ = await asyncio.wait({request}, timeout=1.5)
                assert not done, "a held Responses stream committed headers before its upstream terminal"
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
                cancelled_at = time.monotonic()
            else:
                async with client.post(url, headers=headers, json=body) as response:
                    assert response.status == 200
                    await response.content.readuntil(b"partial")
                    cancelled_at = time.monotonic()
        try:
            lag = round(await asyncio.wait_for(asyncio.shield(upstream_closed), timeout=10) - cancelled_at, 3)
        except asyncio.TimeoutError:
            lag = None
        again = await _post(url, harness.hop, body)
        state = harness.service.store.load().sources[0].state.status
    _record(tmp_path, {"protocol": protocol, "upstream_close_lag_seconds": lag, "next_status": again["status"],
                       "source_status_after": state})
    assert lag is not None and lag < 2, lag
    assert again["status"] == 200, again
    assert state != "cooldown", state


async def test_engine_stop_ends_open_streams_without_a_forced_kill(tmp_path, monkeypatch) -> None:
    """Stopping the engine (service shutdown, runtime disable) must not need SIGKILL while a stream is open."""
    signals: list[int] = []
    original = engine_supervisor.signal_process_tree

    def recording(process, sig, *args, **kwargs):
        signals.append(int(sig))
        return original(process, sig, *args, **kwargs)

    monkeypatch.setattr(engine_supervisor, "signal_process_tree", recording)

    async def handler(request, body):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        frames = _partial_frames("openai_chat")
        try:
            for _ in range(300):
                await response.write(frames[-1])
                await asyncio.sleep(0.2)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            raise
        return response

    async with _through_engine(tmp_path, monkeypatch, "openai_chat", handler) as harness:
        connection = harness.adapter.supervisor.client_if_running().connection
        url = connection.base_url + "/v1/chat/completions"
        body = {"model": f'{harness.adapter.state_store.list_sources()[0].prefix}/{MODEL}', "stream": True,
                "messages": [{"role": "user", "content": "x"}]}
        async with aiohttp.ClientSession() as client:
            async with client.post(url, headers={"Authorization": f"Bearer {connection.gateway_token}"},
                                   json=body) as response:
                assert response.status == 200, await response.text()
                await response.content.readuntil(b"partial")
                started = time.monotonic()
                await asyncio.to_thread(harness.adapter.supervisor.stop)
                stop_seconds = round(time.monotonic() - started, 3)
                try:
                    await asyncio.wait_for(response.content.read(), timeout=5)
                    stream_end = "eof"
                except aiohttp.ClientPayloadError:
                    stream_end = "payload_error"
                except asyncio.TimeoutError:
                    stream_end = "still_open"
    _record(tmp_path, {"signals": signals, "stop_seconds": stop_seconds, "stream_end": stream_end})
    assert signals == [int(signal.SIGTERM)], signals
    assert stream_end != "still_open"


@pytest.mark.parametrize(("model_id", "thinking"), [
    # Claude 4.5 takes a token budget, and refuses adaptive thinking.
    ("claude-sonnet-4-5", {"type": "enabled", "budget_tokens": 4000}),
    # Claude 4.6 and later take adaptive thinking; the engine maps a budget to it.
    ("claude-sonnet-4-6", {"type": "adaptive"}),
])
async def test_budget_thinking_reaches_a_claude_model_as_the_kind_it_takes(tmp_path, monkeypatch, model_id, thinking):
    """#2737: the pinned engine rewrote budget thinking for a Claude 4.5 model into adaptive thinking."""

    async def answer(_request, _body):
        return web.json_response({
            "id": "msg_wire", "type": "message", "role": "assistant", "model": model_id,
            "content": [{"type": "text", "text": "."}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        })

    async with _through_engine(
        tmp_path, monkeypatch, "anthropic", answer, model_id=model_id, efforts=("low", "medium", "high", "max"),
    ) as harness:
        url, body = _frontend_request("anthropic", harness.hop, stream=False, extra={
            "max_tokens": 8000, "thinking": {"type": "enabled", "budget_tokens": 4000},
        })
        observed = await _post(url, harness.hop, body)
    assert observed["status"] == 200, observed
    (sent,) = harness.requests[0]
    assert sent["body"]["thinking"] == thinking
    assert ("output_config" in sent["body"]) is (thinking["type"] == "adaptive")
