"""Native hosted replay and pinning (``vibey-hosted-tools.md`` §3.5, §4.4, §5). Scenario MH-HOSTED-VIBEY-005.

A hosted call goes back verbatim, its native payload in place, only to the Source that served it, on the same origin,
and either within its turn or with the hop's current ``replay`` evidence. Such a request is pinned to that Source; an
in-flight continuation also names the model that served it. When the gateway answers ``pin_unavailable``, the adapter
sends the same request exactly once more, in portable form and unpinned.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.ai.registry import build_wire_request, create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    HostedAction,
    HostedNative,
    HostedRecord,
    HostedSource,
    HostedSummary,
    HostedToolBlock,
    Origin,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from core.agent_core.tools.hosted import hosted_tool_spec

SOURCE = "src_served00001"
OTHER = "src_other000001"
IDENTITY = "sha256:served-account"
WEB = hosted_tool_spec("web_search")
CALL_ITEM = {
    "id": "ws_1",
    "type": "web_search_call",
    "status": "completed",
    "action": {"type": "search", "query": "avibe release", "sources": [{"type": "url", "url": "https://avibe.bot/r"}]},
}
USE = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "avibe release"}}
RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [{"type": "web_search_result", "url": "https://avibe.bot/r", "title": "R", "encrypted_content": "opaque"}],
}
PAYLOADS = {"openai_responses": (CALL_ITEM,), "anthropic": (USE, RESULT)}
MODELS = {
    "openai_responses": ("xai", "grok-4.6"),
    "anthropic": ("anthropic", "claude-opus-5-5"),
    "openai_chat": ("google", "gemini-3-pro"),
}


def _endpoint(protocol: str, *, replay: bool, native: bool = True, source_id: str | None = SOURCE) -> ModelEndpoint:
    provider, model = MODELS[protocol]
    declaration = {"type": "web_search_20250305", "name": "web_search"} if protocol == "anthropic" else {"type": "web_search"}
    route = (
        HostedToolRoute("native", None, replay, declaration=declaration)
        if native
        else HostedToolRoute("proxy", None, replay)
    )
    return ModelEndpoint(
        protocol,  # type: ignore[arg-type]
        "http://hub.invalid/vibey/v1",
        model,
        "hub-token",
        provider=provider,
        source_id=source_id,
        source_identity=IDENTITY,
        hosted_tools={"web_search": route},
    )


def _searched(
    protocol: str, *, served_by: str = SOURCE, stop_reason: str = "stop", extra=(), identity: str = IDENTITY
) -> AssistantMessage:
    provider, model = MODELS[protocol]
    call = HostedToolBlock(
        id="ws_1" if protocol == "openai_responses" else "srvtoolu_1",
        tool="web_search",
        action=HostedAction("search", queries=("avibe release",)),
        record=HostedRecord(status="completed", sources=(HostedSource(url="https://avibe.bot/r"),)),
        native=HostedNative(source_id=served_by, payload=PAYLOADS[protocol]),
    )
    return AssistantMessage(
        (call, TextBlock(text="found it"), *extra),
        Origin(provider, protocol, model),
        stop_reason,
        hosted_summary=HostedSummary(),
        source_identity=identity,
    )


def _request(endpoint: ModelEndpoint, *messages: Any) -> ModelRequest:
    return ModelRequest(
        endpoint=endpoint,
        system="system",
        messages=(UserMessage((TextBlock("search"),)), *messages),
        tools=(WEB,),
        max_tokens=256,
        cache="none",
    )


def _history_items(protocol: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    if protocol == "openai_responses":
        return list(body["input"])
    return [block for message in body["messages"] if isinstance(message["content"], list) for block in message["content"]]


def _verbatim(protocol: str, body: dict[str, Any]) -> bool:
    items = _history_items(protocol, body)
    return all(item in items for item in PAYLOADS[protocol])


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic"])
@pytest.mark.parametrize(
    ("replay", "served_by", "identity", "verbatim"),
    [
        (True, SOURCE, IDENTITY, True),
        (False, SOURCE, IDENTITY, False),
        (True, OTHER, IDENTITY, False),
        # The same Source after its key changed is another account, whose native payload it cannot read.
        (True, SOURCE, "sha256:previous-account", False),
    ],
    ids=["same-source-with-replay-evidence", "no-replay-evidence", "another-source", "another-identity"],
)
async def test_a_hosted_call_goes_back_verbatim_and_pinned_only_to_its_own_source_with_replay_evidence(
    protocol, replay, served_by, identity, verbatim
):
    request = _request(
        _endpoint(protocol, replay=replay, native=False),
        _searched(protocol, served_by=served_by, identity=identity),
        UserMessage((TextBlock("and then?"),)),
    )

    wire = await build_wire_request(request, gateway=True)

    assert _verbatim(protocol, wire.body) is verbatim
    # The request carries no native declaration here (a proxied route), so only a verbatim payload pins it.
    assert (wire.headers.get(PIN_SOURCE_HEADER) == SOURCE) is verbatim
    assert PIN_MODEL_HEADER not in wire.headers


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic"])
@pytest.mark.parametrize(
    ("hop_source", "after", "continued"),
    [
        (SOURCE, (), True),
        (OTHER, (), False),
        (SOURCE, (UserMessage((TextBlock("a new turn"),)),), False),
    ],
    ids=["on-the-hop-that-served-it", "after-the-hop-moved", "after-its-turn-ended"],
)
async def test_a_continuation_goes_back_verbatim_only_to_the_hop_that_served_it(protocol, hop_source, after, continued):
    """Within its turn the provider's own continuation contract applies: no replay evidence is needed, and the request
    is pinned to its hop's Source and the model that served the response it continues. A fresh hop that is another
    Source gets the in-place form instead (§4.4), so one hop builds and serves the request. A turn can end right after
    its tool results; a later turn is no continuation, so without replay evidence its calls go back portable."""

    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    request = _request(
        _endpoint(protocol, replay=False, source_id=hop_source),
        _searched(protocol, stop_reason="tool_use", extra=(read,)),
        ToolResultMessage("toolu_1", "read", (TextBlock("contents"),)),
        *after,
    )

    wire = await build_wire_request(request, gateway=True)

    assert _verbatim(protocol, wire.body) is continued
    # The hop's native declaration pins every case to the hop's own Source.
    assert wire.headers[PIN_SOURCE_HEADER] == hop_source
    assert (wire.headers.get(PIN_MODEL_HEADER) == MODELS[protocol][1]) is continued
    if hop_source == OTHER:
        in_place = json.dumps(wire.body)
        assert '[web_search: \\"avibe release\\"]' in in_place


@pytest.mark.parametrize("model", ["grok-4.6-é", "grok-4.6\nvariant", "grok-4.6\x00", " grok-4.6"])
async def test_a_continuation_of_a_model_whose_id_is_no_header_value_is_pinned_to_its_source_alone(model):
    """A header carries only printable ASCII: the continuation still goes out verbatim, pinned to its hop's Source,
    whose own model the gateway takes."""

    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    searched = _searched("openai_responses", stop_reason="tool_use", extra=(read,))
    endpoint = replace(_endpoint("openai_responses", replay=False), model_id=model)
    request = _request(
        endpoint,
        replace(searched, origin=replace(searched.origin, model=model)),
        ToolResultMessage("toolu_1", "read", (TextBlock("contents"),)),
    )

    wire = await build_wire_request(request, gateway=True)

    assert _verbatim("openai_responses", wire.body)
    assert wire.headers[PIN_SOURCE_HEADER] == SOURCE and PIN_MODEL_HEADER not in wire.headers
    httpx.Request("POST", wire.url, headers=wire.headers, json=wire.body)


_PIN_UNAVAILABLE = {"type": "error", "error": {"type": "model_hub_pin_unavailable", "code": "pin_unavailable"}}
_ANSWERED = {
    "openai_responses": (
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "status": "in_progress", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": "ok"},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "ok", "annotations": []}]},
        },
        {"type": "response.completed", "response": {"status": "completed", "output": None, "usage": {"input_tokens": 5, "output_tokens": 1}}},
    ),
    "anthropic": (
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ),
    "openai_chat": (
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
    ),
}


def _tool_name(tool: dict[str, Any]) -> str:
    return tool.get("name") or tool["function"]["name"]


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic", "openai_chat"])
@pytest.mark.parametrize(
    ("second", "served_by"),
    [("served", OTHER), ("served", SOURCE), ("pin_unavailable", OTHER)],
    ids=["served-by-another-source", "served-by-its-source", "pin_unavailable"],
)
async def test_pin_unavailable_gets_exactly_one_portable_resend_and_never_the_generic_retry(protocol, second, served_by):
    """MH-HOSTED-VIBEY-005: the pinned request carries a native declaration and, where the protocol replays one, a
    verbatim payload. Its one resend carries neither and no pin; that resend's own answer is what the loop sees. A
    resend the hop's own Source serves is still its own: the answer keeps that Source's identity."""

    replayed = protocol in PAYLOADS
    history = (_searched(protocol), UserMessage((TextBlock("and then?"),))) if replayed else ()
    endpoint = _endpoint(protocol, replay=True)
    request = _request(endpoint, *history)
    provider, model = MODELS[protocol]
    served = json.dumps({"provider": provider, "api": protocol, "model": model, "source": served_by}, separators=(",", ":"))
    sent: list[httpx.Request] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        if len(sent) == 1 or second == "pin_unavailable":
            return httpx.Response(409, json=_PIN_UNAVAILABLE)
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in _ANSWERED[protocol])
        body += "data: [DONE]\n\n" if protocol == "openai_chat" else ""
        return httpx.Response(200, headers={"content-type": "text/event-stream", "x-avibe-served-hop": served}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        adapter = create_adapter(protocol, client=client, gateway=True)  # type: ignore[arg-type]
        events = [event async for event in adapter.stream(request, CancelToken())]

    assert len(sent) == 2
    pinned, portable = (json.loads(wire.content) for wire in sent)
    assert sent[0].headers[PIN_SOURCE_HEADER] == SOURCE and PIN_SOURCE_HEADER not in sent[1].headers
    assert pinned["tools"] == [dict(endpoint.hosted_tools["web_search"].declaration)]
    assert [_tool_name(tool) for tool in portable["tools"]] == ["web_search"] and portable["tools"] != pinned["tools"]
    if replayed:
        assert _verbatim(protocol, pinned) and not _verbatim(protocol, portable)
    if second == "served":
        assert isinstance(events[-1], Done)
        assert events[-1].message.source_identity == (IDENTITY if served_by == SOURCE else None)
    else:
        # A 409 to an unpinned request is an ordinary error: there is no second portable resend.
        assert isinstance(events[-1], ProviderError) and events[-1].status == 409
