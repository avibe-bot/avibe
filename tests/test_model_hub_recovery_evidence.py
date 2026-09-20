"""Protocol recovery evidence is stricter than permissive HTTP forwarding."""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace

from aiohttp import web
import pytest

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.stream_wire import (
    ProtocolFactProjector,
    ProtocolObservation,
    ProtocolSSEState,
    observe_buffered_protocol_response,
    observe_protocol_response,
)
from tests.test_model_hub_retry_advice import WIRE, _engine, _invoke, _source
from vibe.model_hub_runtime import client as client_module


BUFFERED = {
    "anthropic": (
        {"type": "message", "role": "assistant"},
        "content",
        [{"type": "text", "text": "已恢复"}],
    ),
    "openai_responses": (
        {"object": "response", "status": "completed"},
        "output",
        [{"type": "message", "content": [{"type": "output_text", "text": "已恢复"}]}],
    ),
    "openai_chat": (
        {"object": "chat.completion"},
        "choices",
        [{"index": 0, "message": {"role": "assistant", "content": "已恢复"}, "finish_reason": "stop"}],
    ),
}
METADATA = {
    "anthropic": b'event: message_start\ndata: {"type":"message_start","message":{"role":"assistant"}}\n\n',
    "openai_responses": b'event: response.created\ndata: {"type":"response.created"}\n\n',
    "openai_chat": b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
}
TERMINAL = {
    "anthropic": b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    "openai_responses": b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
    "openai_chat": b"data: [DONE]\n\n",
}
REASONING = {
    "anthropic": (
        b'event: content_block_delta\ndata: {"type":"content_block_delta",'
        b'"delta":{"type":"thinking_delta","thinking":"reasoning"}}\n\n'
    ),
    "openai_responses": (
        b'event: response.reasoning_text.delta\ndata: {"type":"response.reasoning_text.delta","delta":"reasoning"}\n\n'
    ),
    "openai_chat": b'data: {"choices":[{"delta":{"reasoning_content":"reasoning"}}]}\n\n',
}
TOOL_OUTPUT = {
    "anthropic": (
        b'event: content_block_start\ndata: {"type":"content_block_start",'
        b'"content_block":{"type":"tool_use","name":"example","input":{}}}\n\n'
    ),
    "openai_responses": (
        b'event: response.function_call_arguments.delta\n'
        b'data: {"type":"response.function_call_arguments.delta","delta":"{}"}\n\n'
    ),
    "openai_chat": b'data: {"choices":[{"delta":{"tool_calls":[{"id":"tool-call"}]}}]}\n\n',
}


def _body(protocol: str, *, empty: bool = False) -> bytes:
    selectors, array_path, output = BUFFERED[protocol]
    return json.dumps({**selectors, array_path: [] if empty else output}, ensure_ascii=False).encode()


def test_recovery_evidence_is_additive_and_participates_in_equality() -> None:
    outcome = RawCallOutcome(RawOutcomeKind.SUCCESS, 200, None, None, False, "model-a", "source-a")
    observation = ProtocolObservation(outcome="served")
    assert outcome.recovery_verified is False
    assert observation.recovery_verified is False
    assert replace(outcome, recovery_verified=True) != outcome
    assert replace(observation, recovery_verified=True) != observation


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize("malformed", [False, True], ids=["valid", "nonstandard-json"])
def test_stream_evidence_agrees_between_legacy_and_incremental_observation(protocol: str, malformed: bool) -> None:
    frame = REASONING[protocol]
    event = next(
        (line.removeprefix(b"event: ").decode() for line in frame.splitlines() if line.startswith(b"event: ")),
        None,
    )
    payload = next(line.removeprefix(b"data: ") for line in frame.splitlines() if line.startswith(b"data: "))
    if malformed:
        payload = payload[:-1] + b',"unknown":NaN}'
    legacy = observe_protocol_response(protocol, streamed=True, data=payload, event_name=event)
    projector = ProtocolFactProjector(protocol)
    projector.feed(payload)
    projected = projector.finish(streamed=True, event_name=event)
    assert legacy.recovery_verified is (not malformed)
    assert projected.recovery_verified is legacy.recovery_verified


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize("empty", [False, True], ids=["nonempty", "empty"])
async def test_buffered_native_completion_proves_recovery_without_changing_bytes(
    protocol: str, empty: bool,
) -> None:
    payload = _body(protocol, empty=empty)

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, requests):
        outcome, forwarded = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.recovery_verified is True
    assert outcome.retry_after is None
    assert outcome.response_received_at is None
    assert forwarded == payload
    assert len(requests) == 1
    assert observe_buffered_protocol_response(protocol, io.BytesIO(payload)).recovery_verified is True
    assert observe_protocol_response(protocol, streamed=False, data=payload).recovery_verified is True


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"invalid", id="malformed"),
        pytest.param(b'{"id":', id="truncated-json"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"null", id="null"),
        pytest.param(b"42", id="number"),
        pytest.param(b'"ok"', id="string"),
        pytest.param(b"{}", id="empty-object"),
        pytest.param(b'{"id":"response"}', id="unknown-object"),
        pytest.param(b'{"status":"ok","healthy":true}', id="health-object"),
    ],
)
async def test_unknown_buffered_success_is_forwarded_without_recovery_evidence(protocol: str, payload: bytes) -> None:
    """MH-RETRY-EVIDENCE-001: permissive forwarding cannot prove Source recovery."""
    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, requests):
        outcome, forwarded = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.http_status == 200
    assert outcome.recovery_verified is False
    assert forwarded == payload
    assert len(requests) == 1
    assert observe_buffered_protocol_response(protocol, io.BytesIO(payload)).recovery_verified is False
    assert observe_protocol_response(protocol, streamed=False, data=payload).recovery_verified is False


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize(
    "mutation",
    ["missing-array", "wrong-array-type", "missing-discriminator", "nested-result", "truncated", "nan", "duplicate-array"],
)
async def test_buffered_evidence_requires_the_recognized_shape_and_complete_json(protocol: str, mutation: str) -> None:
    selectors, array_path, _output = BUFFERED[protocol]
    document = json.loads(_body(protocol, empty=True))
    if mutation == "missing-array":
        document.pop(array_path)
    elif mutation == "wrong-array-type":
        document[array_path] = {}
    elif mutation == "missing-discriminator":
        document.pop(next(iter(selectors)))
    elif mutation == "nested-result":
        document = {"result": document}
    payload = json.dumps(document).encode()
    if mutation == "truncated":
        payload = payload[:-1]
    elif mutation == "nan":
        payload = payload[:-1] + b',"unknown":NaN}'
    elif mutation == "duplicate-array":
        payload = payload[:-1] + b',"' + array_path.encode() + b'":null}'

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, _requests):
        outcome, forwarded = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.recovery_verified is False
    assert forwarded == payload
    assert observe_buffered_protocol_response(protocol, io.BytesIO(payload)).recovery_verified is False
    assert observe_protocol_response(protocol, streamed=False, data=payload).recovery_verified is False


@pytest.mark.parametrize("status", ["completed", "incomplete", "queued", "in_progress", "failed", "cancelled"])
async def test_buffered_responses_need_a_completed_status_for_recovery(status: str) -> None:
    payload = json.dumps({"object": "response", "status": status, "output": [], "error": None}).encode()

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, _requests):
        outcome, forwarded = await _invoke(client, "openai_responses", False)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.recovery_verified is (status in {"completed", "incomplete"})
    assert forwarded == payload


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize("failure", ["http", "envelope"])
async def test_failed_response_never_proves_recovery_from_a_success_shaped_body(protocol: str, failure: str) -> None:
    document = json.loads(_body(protocol))
    if failure == "envelope":
        document["error"] = {"type": "server_error"}

    async def respond(_request: web.Request) -> web.Response:
        return web.json_response(document, status=503 if failure == "http" else 200)

    async with _engine(respond) as (client, _requests):
        outcome, forwarded = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.HTTP_ERROR
    assert outcome.recovery_verified is False
    assert forwarded == b""


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize(
    "body_mode",
    ["output", "reasoning", "tool", "empty-completion", "metadata", "heartbeat", "no-event", "unknown", "error"],
)
async def test_streaming_recovery_uses_only_recognized_output_or_success(protocol: str, body_mode: str) -> None:
    payload = {
        "output": WIRE[protocol][2],
        "reasoning": REASONING[protocol] + TERMINAL[protocol],
        "tool": TOOL_OUTPUT[protocol] + TERMINAL[protocol],
        "empty-completion": METADATA[protocol] + TERMINAL[protocol],
        "metadata": METADATA[protocol],
        "heartbeat": b": keepalive\n\nevent: ping\n\n",
        "no-event": b"",
        "unknown": b'data: {"unknown":"shape"}\n\n',
        "error": WIRE[protocol][3],
    }[body_mode]
    verified = body_mode in {"output", "reasoning", "tool", "empty-completion"}

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="text/event-stream")

    async with _engine(respond) as (client, requests):
        outcome, forwarded = await _invoke(client, protocol, True)

    assert outcome.recovery_verified is verified
    if verified:
        assert outcome.kind is RawOutcomeKind.SUCCESS
        assert outcome.stream_started is (body_mode != "empty-completion")
        assert forwarded == payload
    else:
        assert outcome.kind is (RawOutcomeKind.HTTP_ERROR if body_mode == "error" else RawOutcomeKind.NETWORK_ERROR)
        assert forwarded == b""
    assert len(requests) == 1

    state = ProtocolSSEState(protocol)
    state.observe(payload)
    terminal = state.terminal_observation()
    if terminal is not None:
        assert terminal.recovery_verified is verified


@pytest.mark.parametrize("protocol", BUFFERED)
@pytest.mark.parametrize("ending", ["success", "error", "disconnect"])
async def test_model_output_proves_recovery_before_stream_terminal(protocol: str, ending: str) -> None:
    release_terminal = asyncio.Event()

    async def respond(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(REASONING[protocol])
        await release_terminal.wait()
        if ending == "disconnect":
            assert request.transport is not None
            request.transport.close()
        else:
            await response.write(TERMINAL[protocol] if ending == "success" else WIRE[protocol][3])
            await response.write_eof()
        return response

    async with _engine(respond) as (client, requests):
        handle = await asyncio.wait_for(client.invoke(_source(protocol), "model-a", {}, stream=True), timeout=2)
        try:
            assert handle.observed is not None
            assert handle.observed.model_output_started is True
            assert handle.observed.terminal_outcome is None
            assert handle.outcome_available is False
            release_terminal.set()
            forwarded = b"".join([chunk async for chunk in handle.stream])
            outcome = await handle.outcome()
        finally:
            release_terminal.set()
            await handle.close_stream()

    assert outcome.kind is {
        "success": RawOutcomeKind.SUCCESS,
        "error": RawOutcomeKind.HTTP_ERROR,
        "disconnect": RawOutcomeKind.NETWORK_ERROR,
    }[ending]
    assert outcome.recovery_verified is True
    assert outcome.stream_started is True
    assert forwarded.startswith(REASONING[protocol])
    assert len(requests) == 1


@pytest.mark.parametrize("protocol", BUFFERED)
async def test_large_buffered_result_preserves_bytes_and_bounded_evidence_projection(
    monkeypatch: pytest.MonkeyPatch, protocol: str,
) -> None:
    # Force the existing spool path; neither the new evidence nor a late native
    # discriminator may impose a response size limit or keep the body in memory.
    monkeypatch.setattr(client_module, "_PRELUDE_MEMORY_BYTES", 64)
    payload = b'{"unknown":"' + b"x" * (1024 * 1024) + b'",' + _body(protocol)[1:]
    projector = ProtocolFactProjector(protocol)
    for offset in range(0, len(payload), 4096):
        projector.feed(payload[offset:offset + 4096])
        assert projector.retained_bytes < 32 * 1024
    assert projector.finish(streamed=False).recovery_verified is True

    async def respond(_request: web.Request) -> web.Response:
        return web.Response(body=payload, content_type="application/json")

    async with _engine(respond) as (client, _requests):
        outcome, forwarded = await _invoke(client, protocol, False)

    assert outcome.kind is RawOutcomeKind.SUCCESS
    assert outcome.recovery_verified is True
    assert forwarded == payload
