"""Anthropic ``pause_turn`` (``vibey-hosted-tools.md`` §4.3). Scenario MH-HOSTED-VIBEY-006.

A paused response is not a stop. The adapter sends its content back as it is, with no new user message, pinned to the
Source and model that served it, and keeps streaming the same response: one message of one origin, its pending call
finalized by id, its hosted counters summed. Past five continuations, or when that Source cannot continue it, it ends
``interrupted`` and keeps what it has.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Callable

import httpx
import pytest

from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER, portable_endpoint
from core.agent_core.ai.anthropic import (
    MAX_PAUSE_CONTINUATIONS,
    PAUSE_ALTERED_MESSAGE,
    PAUSE_LIMIT_MESSAGE,
    PAUSE_UNSERVED_MESSAGE,
)
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.ai.registry import create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    MAX_HOSTED_COUNT,
    HostedToolBlock,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    Usage,
    UserMessage,
)
from core.agent_core.tools.hosted import hosted_tool_spec

SOURCE = "src_served00001"
MODEL = "claude-opus-5-5"
DECLARATION = {"type": "web_search_20250305", "name": "web_search"}
USE = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{"type": "web_search_result", "url": "https://avibe.bot/r", "title": "R", "encrypted_content": "opaque"}],
}
_PIN_UNAVAILABLE = {"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}


def _request(*, cache: str = "none", base_url: str = "http://hub.invalid/vibey/v1") -> ModelRequest:
    route = HostedToolRoute("native", False, declaration=DECLARATION)
    return ModelRequest(
        endpoint=ModelEndpoint(
            "anthropic",
            base_url,
            MODEL,
            "hub-token",
            provider="anthropic",
            source_id=SOURCE,
            hosted_tools={"web_search": route},
        ),
        system="system",
        messages=(UserMessage((TextBlock("search, then search again"),)),),
        tools=(hosted_tool_spec("web_search"),),
        max_tokens=256,
        cache=cache,  # type: ignore[arg-type]
    )


def _response(
    *blocks: list[dict[str, Any]],
    stop_reason: str,
    input_tokens: int,
    output_tokens: int,
    searches: int = 0,
    framed: bool = True,
) -> str:
    """A Messages stream; one not ``framed`` has no ``message_start`` or ``message_stop`` and ends at a clean EOF."""

    events: list[dict[str, Any]] = [{"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}}]
    for index, frames in enumerate(blocks):
        events.extend({**frame, "index": index} for frame in frames)
        events.append({"type": "content_block_stop", "index": index})
    usage: dict[str, Any] = {"output_tokens": output_tokens}
    if searches:
        usage["server_tool_use"] = {"web_search_requests": searches}
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": usage})
    events.append({"type": "message_stop"})
    if not framed:
        events = events[1:-1]
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


def _use() -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "content_block": USE},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"query": "avibe release"}'}},
    ]


def _text(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    ]


def _thinking() -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "plan"}},
        {"type": "content_block_delta", "delta": {"type": "signature_delta", "signature": "signed-by-opus"}},
    ]


def _served(model: str = MODEL, source: str = SOURCE, *, reported: bool = True) -> dict[str, str]:
    if not reported:
        return {"content-type": "text/event-stream"}
    report = {"provider": "anthropic", "api": "anthropic", "model": model, "source": source}
    return {"content-type": "text/event-stream", "x-avibe-served-hop": json.dumps(report, separators=(",", ":"))}


async def _stream(
    answer: Callable[[int], httpx.Response], request: ModelRequest | None = None, media_loader: Any = None
) -> tuple[list[httpx.Request], list[Any]]:
    sent: list[httpx.Request] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        return answer(len(sent))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        adapter = create_adapter("anthropic", client=client, gateway=True, media_loader=media_loader)
        events = [event async for event in adapter.stream(request or _request(), CancelToken())]
    return sent, events


@pytest.mark.parametrize("framed", [True, False], ids=["framed", "clean-eof"])
async def test_a_paused_response_continues_as_one_response_pinned_to_its_source_with_summed_counters(framed):
    """MH-HOSTED-VIBEY-006: the continuation sends the paused content back as it is, with no new user message, pinned
    to the Source and model that served it; the result that arrives finalizes the pending call by its id. A paused
    stream that ends at a clean EOF continues as one that sent ``message_stop``."""

    def answer(number: int) -> httpx.Response:
        if number == 1:
            body = _response(
                _use(), stop_reason="pause_turn", input_tokens=100, output_tokens=10, searches=1, framed=framed
            )
        else:
            body = _response([{"type": "content_block_start", "content_block": RESULT}], _text("v8 is out"),
                             stop_reason="end_turn", input_tokens=300, output_tokens=20, searches=1)
        return httpx.Response(200, headers=_served(), text=body)

    sent, events = await _stream(answer)

    assert len(sent) == 2
    first, continued = (json.loads(wire.content) for wire in sent)
    assert continued["messages"][:-1] == first["messages"]
    assert continued["messages"][-1] == {
        "role": "assistant",
        "content": [{**USE, "input": {"query": "avibe release"}}],
    }
    assert sent[1].headers[PIN_SOURCE_HEADER] == SOURCE and sent[1].headers[PIN_MODEL_HEADER] == MODEL
    message = events[-1].message
    assert isinstance(events[-1], Done) and message.stop_reason == "stop"
    (call,) = [block for block in message.content if isinstance(block, HostedToolBlock)]
    assert call.id == "srvtoolu_1" and call.record.status == "completed"
    assert [source.url for source in call.record.sources] == ["https://avibe.bot/r"]
    assert call.native.payload == ({**USE, "input": {"query": "avibe release"}}, RESULT)
    assert [block.text for block in message.content if isinstance(block, TextBlock)] == ["v8 is out"]
    # The final request already re-sent every paused part: its usage is the response's context (§4.3 as implemented).
    assert message.usage == Usage(input_tokens=300, output_tokens=20)
    assert dict(message.hosted_summary.counts) == {"web_search_requests": 2}
    assert not [event for event in events if isinstance(event, ProviderError)]


@pytest.mark.parametrize(
    "searches",
    [
        (MAX_HOSTED_COUNT, MAX_HOSTED_COUNT, 1),
        (10**400, 1),
    ],
    ids=["total-past-the-bound", "one-response-past-the-bound"],
)
async def test_a_counter_past_the_bound_in_any_pause_stays_absent_and_the_response_completes(searches):
    """MH-HOSTED-VIBEY-006 (#2572): each response's counts are bounded, but not their total across pauses. A counter
    whose total passes the bound, in one response or summed over several, is no count, and a later pause cannot bring
    back a smaller one; it never fails the turn."""

    def answer(number: int) -> httpx.Response:
        if number < len(searches):
            body = _response(
                _use(), stop_reason="pause_turn", input_tokens=100, output_tokens=10, searches=searches[number - 1]
            )
        else:
            body = _response([{"type": "content_block_start", "content_block": RESULT}], _text("v8 is out"),
                             stop_reason="end_turn", input_tokens=300, output_tokens=20, searches=searches[-1])
        return httpx.Response(200, headers=_served(), text=body)

    sent, events = await _stream(answer)

    assert len(sent) == len(searches)
    assert isinstance(events[-1], Done) and events[-1].message.stop_reason == "stop"
    assert dict(events[-1].message.hosted_summary.counts) == {}


async def test_a_continuation_that_reports_no_usage_leaves_the_message_without_the_paused_requests():
    """The merged message's usage stands for the final request; a final response that reports none leaves it unknown,
    never the smaller paused request's."""

    def answer(number: int) -> httpx.Response:
        if number == 1:
            body = _response(_text("part 1"), stop_reason="pause_turn", input_tokens=100, output_tokens=10)
        else:
            events = [
                {"type": "message_start", "message": {}},
                *({**frame, "index": 0} for frame in _text("done")),
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers=_served(), text=body)

    sent, events = await _stream(answer)

    message = events[-1].message
    assert len(sent) == 2 and isinstance(events[-1], Done)
    assert [block.text for block in message.content if isinstance(block, TextBlock)] == ["part 1", "done"]
    assert message.usage is None


async def test_a_response_paused_past_the_bound_ends_interrupted_and_keeps_everything_it_assembled():
    def answer(number: int) -> httpx.Response:
        body = _response(_text(f"step {number}"), stop_reason="pause_turn", input_tokens=10, output_tokens=1)
        return httpx.Response(200, headers=_served(), text=body)

    sent, events = await _stream(answer)

    assert len(sent) == 1 + MAX_PAUSE_CONTINUATIONS
    error = events[-1]
    assert isinstance(error, ProviderError) and error.kind == "interrupted" and error.message == PAUSE_LIMIT_MESSAGE
    assert [block.text for block in error.partial.content] == [f"step {n}" for n in range(1, len(sent) + 1)]
    assert error.partial.usage == Usage(input_tokens=10, output_tokens=1)
    assert sum(isinstance(event, (Done, ProviderError)) for event in events) == 1


@pytest.mark.parametrize("refused", ["hosted", "signature"])
async def test_a_paused_response_the_session_cannot_send_back_as_served_ends_interrupted_without_a_continuation(
    refused,
):
    """§4.3: a continuation is the first send plus the paused response exactly as served. When the Session can no
    longer send part of it back, because the provider refused that identity's hosted payloads or thinking signatures,
    the continuation would be a different request ending in an assistant turn the provider takes as a prefill. The
    response ends ``interrupted`` with what it has, and nothing more is sent."""

    identity = "sha256:served"
    request = _request()
    request = replace(
        request,
        endpoint=replace(request.endpoint, source_identity=identity, opaque_refused=frozenset({(identity, refused)})),
    )

    def answer(number: int) -> httpx.Response:
        body = _response(_thinking(), _use(), stop_reason="pause_turn", input_tokens=100, output_tokens=10, searches=1)
        return httpx.Response(200, headers=_served(), text=body)

    sent, events = await _stream(answer, request)

    assert len(sent) == 1
    error = events[-1]
    assert isinstance(error, ProviderError) and error.kind == "interrupted" and error.message == PAUSE_ALTERED_MESSAGE
    assert [type(block) for block in error.partial.content] == [ThinkingBlock, HostedToolBlock]
    assert sum(isinstance(event, (Done, ProviderError)) for event in events) == 1


@pytest.mark.parametrize(
    ("unavailable", "reported", "portable", "sends"),
    [(2, True, False, 2), (None, True, True, 1), (None, False, False, 1)],
    ids=["continuation-pin-unavailable", "paused-unpinned-send", "paused-unverified-response"],
)
async def test_a_paused_response_its_source_cannot_continue_ends_interrupted_and_keeps_what_it_has(
    unavailable, reported, portable, sends
):
    """§4.3 as implemented: a continuation goes only to the Source that served the paused response, pinned, so the
    response stays one origin's message. It ends without another send when:
    - that Source answers the continuation ``pin_unavailable``: the response has output, so the loop never renders it
      again (§3.5);
    - the paused send carried no pin (a portable request);
    - the paused response named no Source, so its hosted calls could not be kept to send back."""

    def answer(number: int) -> httpx.Response:
        if number == unavailable:
            return httpx.Response(409, json=_PIN_UNAVAILABLE)
        body = _response(_text(f"part {number}"), stop_reason="pause_turn", input_tokens=10, output_tokens=1)
        return httpx.Response(200, headers=_served(reported=reported), text=body)

    request = _request()
    if portable:
        request = replace(request, endpoint=portable_endpoint(request.endpoint))
    sent, events = await _stream(answer, request)

    assert len(sent) == sends
    error = events[-1]
    assert isinstance(error, ProviderError) and error.kind == "interrupted" and error.message == PAUSE_UNSERVED_MESSAGE
    assert error.refusal is None
    assert [block.text for block in error.partial.content if isinstance(block, TextBlock)] == ["part 1"]
    assert sum(isinstance(event, (Done, ProviderError)) for event in events) == 1


async def test_a_continuation_whose_route_refuses_the_cache_lifetime_is_sent_again_and_keeps_what_it_has():
    """The 1-hour cache lifetime a continuation's route refuses is dropped and the same continuation sent once more,
    as for a first request; the response keeps its paused content."""

    rejection = {"type": "error", "error": {"type": "invalid_request_error", "message": "system.0.cache_control.ttl: Extra inputs are not permitted"}}

    def answer(number: int) -> httpx.Response:
        if number == 2:
            return httpx.Response(400, json=rejection)
        stop = "pause_turn" if number == 1 else "end_turn"
        body = _response(_text(f"part {number}"), stop_reason=stop, input_tokens=10, output_tokens=1)
        return httpx.Response(200, headers=_served(), text=body)

    import uuid

    request = _request(cache="default", base_url=f"http://{uuid.uuid4().hex}.invalid/vibey/v1")
    sent, events = await _stream(answer, request)

    first, refused, retried = (json.loads(wire.content) for wire in sent)
    assert '"ttl"' in json.dumps(refused) and '"ttl"' not in json.dumps(retried)
    assert refused["messages"] == retried["messages"]
    message = events[-1].message
    assert [block.text for block in message.content if isinstance(block, TextBlock)] == ["part 1", "part 3"]
    assert message.usage == Usage(input_tokens=10, output_tokens=1)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({}, {"reported": False}),
        ({}, {"model": "claude-sonnet-5-5"}),
    ],
    ids=["then-unverified", "another-origin"],
)
async def test_a_continued_response_is_verified_only_when_every_response_in_it_was_from_one_origin(first, second):
    """A continuation with no served-hop report, or one another origin served, makes the whole message unverified: no
    signature of any part is kept, the paused part's included."""

    def answer(number: int) -> httpx.Response:
        if number == 1:
            body = _response(_thinking(), _text("part 1"), stop_reason="pause_turn", input_tokens=10, output_tokens=1)
            return httpx.Response(200, headers=_served(**first), text=body)
        body = _response(_thinking(), _text("done"), stop_reason="end_turn", input_tokens=10, output_tokens=1)
        return httpx.Response(200, headers=_served(**second), text=body)

    sent, events = await _stream(answer)

    message = events[-1].message
    assert len(sent) == 2 and isinstance(events[-1], Done)
    thinking = [block for block in message.content if isinstance(block, ThinkingBlock)]
    assert len(thinking) == 2 and all(block.signature is None for block in thinking)


async def test_a_paused_response_whose_continuation_cannot_be_prepared_keeps_what_it_streamed():
    """A continuation that fails before it is sent (here an image of the turn that no longer loads) ends the response
    with an error that keeps the paused part, as a failed continuation request does."""

    loads = []

    class Media:
        async def load(self, media_token: str) -> tuple[bytes, str]:
            loads.append(media_token)
            if len(loads) > 1:
                raise RuntimeError("media snapshot is gone")
            return b"\x89PNG\r\n\x1a\n", "image/png"

    def answer(number: int) -> httpx.Response:
        body = _response(_text("part 1"), stop_reason="pause_turn", input_tokens=10, output_tokens=1)
        return httpx.Response(200, headers=_served(), text=body)

    request = _request()
    request = replace(
        request,
        messages=(UserMessage((TextBlock("look and search"), ImageBlock("image/png", "tok_1"))),),
        supports_images=True,
    )
    sent, events = await _stream(answer, request, media_loader=Media())

    error = events[-1]
    assert len(sent) == 1 and isinstance(error, ProviderError)
    assert [block.text for block in error.partial.content if isinstance(block, TextBlock)] == ["part 1"]
    assert error.partial.usage == Usage(input_tokens=10, output_tokens=1)
