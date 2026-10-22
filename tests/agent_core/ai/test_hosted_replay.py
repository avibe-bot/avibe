"""Native hosted replay and pinning (``vibey-hosted-tools.md`` §3.5, §4.4, §5). Scenario MH-HOSTED-VIBEY-005.

A hosted call goes back verbatim, its native payload in place, only to the Source that served it, on the same origin,
and either within its turn or with the hop's current ``replay`` evidence. Such a request is pinned to that Source; an
in-flight continuation also names the model that served it. A refusal before any output, the gateway's
``pin_unavailable`` or xAI's decrypt refusal, ends the send; the loop renders the request again within its bounds.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Optional

import httpx
import pytest

from core.agent_core.agent.events import RunEnded
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import ModelSelection
from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER
from core.agent_core.ai.provider import (
    HostedToolRoute,
    ModelCapabilities,
    ModelEndpoint,
    ModelRequest,
    ProviderError,
    Refusal,
)
from core.agent_core.ai.registry import create_adapter
from tests.agent_core.ai._wire import build_wire_request
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
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)
from core.agent_core.tools.hosted import hosted_tool_spec
from tests.agent_core.fakes import FakeJobHost, FakeTool, InMemoryTranscriptStore, input_row

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
        HostedToolRoute("native", replay, declaration=declaration)
        if native
        else HostedToolRoute("proxy", replay)
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
        _endpoint(protocol, replay=replay),
        _searched(protocol, served_by=served_by, identity=identity),
        UserMessage((TextBlock("and then?"),)),
    )

    wire = await build_wire_request(request, gateway=True)

    assert _verbatim(protocol, wire.body) is verbatim
    # A payload goes back verbatim only where the tool is declared natively, so the request is pinned either way.
    assert wire.headers[PIN_SOURCE_HEADER] == SOURCE
    assert PIN_MODEL_HEADER not in wire.headers


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic"])
@pytest.mark.parametrize(
    ("hop_source", "native", "after", "continued"),
    [
        (SOURCE, True, (), True),
        (OTHER, True, (), False),
        # A refusal's fresh hop on the same Source that no longer runs the tool natively (§3.5).
        (SOURCE, False, (), False),
        (SOURCE, True, (UserMessage((TextBlock("a new turn"),)),), False),
    ],
    ids=["on-the-hop-that-served-it", "after-the-hop-moved", "after-the-tool-turned-proxy", "after-its-turn-ended"],
)
async def test_a_continuation_goes_back_verbatim_only_to_the_hop_that_served_it(protocol, hop_source, native, after, continued):
    """Within its turn the provider's own continuation contract applies: no replay evidence is needed, and the request
    is pinned to its hop's Source and the model that served the response it continues. A fresh hop that is another
    Source, or that no longer declares the tool natively, gets the in-place form instead (§4.4), since a provider
    refuses server items without their declaration. A turn can end right after its tool results; a later turn is no
    continuation, so without replay evidence its calls go back portable."""

    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    request = _request(
        _endpoint(protocol, replay=False, native=native, source_id=hop_source),
        _searched(protocol, stop_reason="tool_use", extra=(read,)),
        ToolResultMessage("toolu_1", "read", (TextBlock("contents"),)),
        *after,
    )

    wire = await build_wire_request(request, gateway=True)

    assert _verbatim(protocol, wire.body) is continued
    # The hop's native declaration pins the request to the hop's own Source; without one nothing pins it.
    assert wire.headers.get(PIN_SOURCE_HEADER) == (hop_source if native else None)
    assert (wire.headers.get(PIN_MODEL_HEADER) == MODELS[protocol][1]) is continued
    if not continued and not after:
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


def _sse(protocol: str, events: tuple[dict[str, Any], ...]) -> str:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return body + ("data: [DONE]\n\n" if protocol == "openai_chat" else "")


def _served(protocol: str, source: str = SOURCE) -> dict[str, str]:
    provider, model = MODELS[protocol]
    report = {"provider": provider, "api": protocol, "model": model, "source": source}
    return {"content-type": "text/event-stream", "x-avibe-served-hop": json.dumps(report, separators=(",", ":"))}


async def _send(request: ModelRequest, answer: Any) -> tuple[list[httpx.Request], list[Any]]:
    sent: list[httpx.Request] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        return answer(len(sent))

    protocol = request.endpoint.protocol
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        adapter = create_adapter(protocol, client=client, gateway=True)
        events = [event async for event in adapter.stream(request, CancelToken())]
    return sent, events


def _hosted_unsupported(tool: str = "web_search", aspect: str = "call", source_id: str = SOURCE) -> dict[str, Any]:
    reason = {"kind": "hosted_unsupported", "tool": tool, "aspect": aspect, "source_id": source_id}
    return {"type": "error", "error": {**_PIN_UNAVAILABLE["error"], "message": "refused", "reason": reason}}


@pytest.mark.parametrize("protocol", ["openai_responses", "anthropic", "openai_chat"])
@pytest.mark.parametrize(
    ("native", "body", "refusal"),
    [
        (True, _PIN_UNAVAILABLE, Refusal("pin_unavailable", SOURCE, IDENTITY)),
        (True, _hosted_unsupported(), Refusal("pin_unavailable", SOURCE, IDENTITY, "web_search", "call")),
        # A 409 to an unpinned request is the gateway's ordinary error: nothing was handed back.
        (False, _PIN_UNAVAILABLE, None),
    ],
    ids=["no-reason", "narrow-rule-reason", "unpinned"],
)
async def test_pin_unavailable_ends_the_pinned_send_as_a_refusal_with_no_output(protocol, native, body, refusal):
    """MH-HOSTED-VIBEY-005: the gateway's hand-back, matched on ``type`` and ``code`` with any other field tolerated,
    ends the pinned send with one error that carries the refusal, the narrow rule when one fired, and no output. The
    adapter sends nothing more and the generic retry never takes it: the loop renders the request again (§3.5)."""

    request = _request(_endpoint(protocol, replay=True, native=native))

    sent, events = await _send(request, lambda _: httpx.Response(409, json=body))

    assert len(sent) == 1 and (PIN_SOURCE_HEADER in sent[0].headers) is native
    (error,) = events
    assert isinstance(error, ProviderError) and error.status == 409 and error.partial is None
    assert error.refusal == refusal
    if refusal is not None:
        assert not error.retryable


_DECRYPT_REFUSAL = "Could not decrypt the provided encrypted_content"
# Anthropic's, which name what they refused.
_SEARCH_REFUSAL = "messages.9.content.0: Invalid `encrypted_content` in `search_result` block"
_SIGNATURE_REFUSAL = "messages.1.content.0: Invalid `signature` in `thinking` block"


@pytest.mark.parametrize("native", [True, False], ids=["pinned", "unpinned"])
@pytest.mark.parametrize(
    ("protocol", "body", "payloads"),
    [
        # xAI's says neither which ciphertext nor which kind.
        ("openai_responses", _DECRYPT_REFUSAL, ("hosted", "signature")),
        # A relay may wrap the provider's text in its own error object.
        (
            "openai_responses",
            json.dumps({"error": {"message": _DECRYPT_REFUSAL, "type": "invalid_request_error"}}),
            ("hosted", "signature"),
        ),
        # Anthropic's names what it refused.
        ("anthropic", json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": _SEARCH_REFUSAL}}),
         ("hosted",)),
        ("anthropic", json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                              "message": "messages.3.content.1: Invalid `encrypted_index` in citation"}}),
         ("hosted",)),
        ("anthropic", json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": _SIGNATURE_REFUSAL}}),
         ("signature",)),
        ("openai_responses", "Invalid request: unknown parameter", ()),
    ],
    ids=["xai-plain-text", "xai-wrapped", "anthropic-search-result", "anthropic-citation-index",
         "anthropic-thinking-signature", "another-400"],
)
async def test_an_opaque_payload_refusal_ends_the_send_as_a_refusal_of_its_source_identity(native, protocol, body, payloads):
    """A provider's 400 for a replayed opaque payload its account cannot read: the adapter, the one classifier, ends
    the send with an opaque refusal of the request's Source identity and the kinds the provider named, pinned or not
    (§3.5). Any other 400 is the ordinary error."""

    request = _request(_endpoint(protocol, replay=True, native=native))

    sent, events = await _send(request, lambda _: httpx.Response(400, text=body))

    (error,) = events
    assert len(sent) == 1 and isinstance(error, ProviderError) and error.status == 400 and not error.retryable
    expected = Refusal("opaque_refused", SOURCE, IDENTITY, payloads=payloads) if payloads else None
    assert error.refusal == expected


# The loop answers a refusal (§3.5). These run a turn through the Agent loop and the real adapters against a stub
# gateway; each resolution is the next hop of the run.

THIRD = "src_third000001"
CAPABILITIES = ModelCapabilities(context_window=200_000, max_output_tokens=4096, supports_tools=True)
X_DECLARATION = {"type": "x_search"}
READ = FakeTool("read")


class _Hops:
    def __init__(
        self, client: httpx.AsyncClient, hops: tuple[ModelEndpoint, ...], capabilities: ModelCapabilities = CAPABILITIES
    ) -> None:
        self.client, self.hops, self.resolved, self.capabilities = client, hops, 0, capabilities

    async def resolve(self) -> ModelSelection:
        hop = self.hops[min(self.resolved, len(self.hops) - 1)]
        self.resolved += 1
        return ModelSelection(hop, self.capabilities)

    def provider_for(self, protocol: str) -> Any:
        return create_adapter(protocol, client=self.client, gateway=True)  # type: ignore[arg-type]


def _hosted_tool(name: str) -> FakeTool:
    tool = FakeTool(name)
    tool.spec = hosted_tool_spec(name)
    return tool


async def _turn(answer: Any, *hops: ModelEndpoint, tools: tuple[Any, ...]) -> tuple[list[httpx.Request], Any, int]:
    """One turn; ``answer(n, body)`` answers the n-th request the gateway receives."""

    sent: list[httpx.Request] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent.append(wire)
        return answer(len(sent), json.loads(wire.content))

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        router = _Hops(client, hops)
        agent = Agent(
            session_id="session",
            models=router,
            tools=tools,
            hooks=(),
            store=InMemoryTranscriptStore(),
            jobs=FakeJobHost(),
            cwd="/test-owned",
        )
        events = [event async for event in agent.run(input_row("input", "search"), turn_id="turn")]
    ended = events[-1]
    assert isinstance(ended, RunEnded)
    return sent, ended, router.resolved


def _xai(source_id: str = SOURCE, *, web: bool = True, model: str = "grok-4.6") -> ModelEndpoint:
    """An xAI hop: ``x_search`` native, ``web_search`` native while ``web`` holds."""

    web_route = HostedToolRoute("native", True, declaration={"type": "web_search"}) if web else HostedToolRoute("proxy", False)
    return replace(
        _endpoint("openai_responses", replay=True, source_id=source_id),
        model_id=model,
        hosted_tools={"web_search": web_route, "x_search": HostedToolRoute("native", True, declaration=X_DECLARATION)},
    )


def _answered(number: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, headers=_served("openai_responses"), text=_sse("openai_responses", _ANSWERED["openai_responses"]))


async def test_a_narrow_refusal_is_answered_by_a_fresh_hop_that_runs_only_that_tool_as_its_function():
    """MH-HOSTED-VIBEY-005: the gateway refuses the pinned request because ``web_search`` cannot run natively there and
    writes that evidence; the fresh hop says so. The request is rendered again for it: ``web_search`` as its function,
    ``x_search`` still native and the request still pinned. No output was lost and nothing waited for a retry."""

    def answer(number: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(409, json=_hosted_unsupported()) if number == 1 else _answered(number, body)

    sent, ended, resolved = await _turn(
        answer, _xai(), _xai(web=False), tools=(_hosted_tool("web_search"), _hosted_tool("x_search"))
    )

    assert ended.reason == "completed" and resolved == 2 and len(sent) == 2
    first, second = (json.loads(wire.content)["tools"] for wire in sent)
    assert first == [{"type": "web_search"}, X_DECLARATION]
    assert second[0]["name"] == "web_search" and second[1] == X_DECLARATION
    assert sent[1].headers[PIN_SOURCE_HEADER] == SOURCE


@pytest.mark.parametrize(
    ("hops", "refusals", "pins", "resolved"),
    [
        # The fresh hop is the refused one again: the same request would go out, so it goes portable at once.
        ((_xai(), _xai()), (_PIN_UNAVAILABLE,), (SOURCE, None), 2),
        # One fresh hop per (Source, tool, aspect): the same rule refusing again on another model there goes portable.
        ((_xai(), _xai(model="grok-4.6-fast")), (_hosted_unsupported(),) * 2, (SOURCE, SOURCE, None), 2),
        # At most two fresh hops in a turn, whatever the reason: a third refusal goes portable on the hop it has.
        ((_xai(), _xai(OTHER), _xai(THIRD)), (_PIN_UNAVAILABLE,) * 3, (SOURCE, OTHER, THIRD, None), 3),
    ],
    ids=["same-hop", "per-rule", "per-turn"],
)
async def test_refusals_are_answered_within_their_bounds_and_portable_rendering_is_the_last_resort(
    hops, refusals, pins, resolved
):
    """A portable request is never pinned, so the gateway never hands it back: every refusal chain ends."""

    def answer(number: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(409, json=refusals[number - 1]) if number <= len(refusals) else _answered(number, body)

    sent, ended, resolutions = await _turn(answer, *hops, tools=(_hosted_tool("web_search"), _hosted_tool("x_search")))

    assert ended.reason == "completed" and resolutions == resolved
    assert tuple(wire.headers.get(PIN_SOURCE_HEADER) for wire in sent) == pins
    portable = json.loads(sent[-1].content)["tools"]
    assert [_tool_name(tool) for tool in portable] == ["web_search", "x_search"]


def _reasoned(cipher: str, *, call: str | None) -> str:
    """An xAI response: a reasoning item carrying ``cipher``, then a ``read`` call or a final answer."""

    reasoning = {"id": f"rs_{cipher}", "type": "reasoning", "summary": [], "encrypted_content": cipher}
    if call is None:
        last = {"id": "msg_1", "type": "message", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}
    else:
        last = {"id": f"fc_{call}", "type": "function_call", "call_id": call, "name": "read", "arguments": "{}"}
    events = (
        {"type": "response.output_item.added", "output_index": 0, "item": {**reasoning, "encrypted_content": None}},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.output_item.added", "output_index": 1, "item": {**last, "status": "in_progress"}},
        {"type": "response.output_item.done", "output_index": 1, "item": last},
        {"type": "response.completed", "response": {"status": "completed", "output": None, "usage": {"input_tokens": 5, "output_tokens": 1}}},
    )
    return _sse("openai_responses", events)


@pytest.mark.parametrize("again", [False, True], ids=["answered", "refused-again"])
async def test_a_decrypt_refusal_drops_every_opaque_payload_of_that_identity_for_the_rest_of_the_turn(again):
    """xAI's encrypted reasoning rides an ordinary, unpinned request. Refused, the request is rendered again without any
    of that identity's ciphertext, and every later request of the turn too, including ciphertext issued after the
    refusal: the refusal does not say which one failed. That is answered once per identity; refused again, the error
    stands."""

    def answer(number: int, body: dict[str, Any]) -> httpx.Response:
        if number == 2 or (number == 3 and again):
            return httpx.Response(400, text=_DECRYPT_REFUSAL)
        reply = {1: _reasoned("cipher-1", call="call_1"), 3: _reasoned("cipher-3", call="call_3")}.get(number)
        return httpx.Response(200, headers=_served("openai_responses"), text=reply or _reasoned("cipher-4", call=None))

    unpinned = replace(_endpoint("openai_responses", replay=False, native=False), hosted_tools={})
    sent, ended, _ = await _turn(answer, unpinned, tools=(READ,))

    bodies = [wire.content.decode() for wire in sent]
    assert all(PIN_SOURCE_HEADER not in wire.headers for wire in sent)
    assert "cipher-1" in bodies[1]
    assert not any(cipher in body for body in bodies[2:] for cipher in ("cipher-1", "cipher-3"))
    if again:
        assert len(sent) == 3 and ended.reason == "error"
    else:
        assert len(sent) == 4 and ended.reason == "completed"


# A relay pooling upstream accounts behind one API key can serve a later turn from another account, which cannot read
# what the first minted. Anthropic's real refusals, as the provider writes them and as the Model Hub gateway forwards
# them (its own code, the upstream detail inside its copy).


def _anthropic_refusal(detail: str, *, via_gateway: bool) -> dict[str, Any]:
    from tests.agent_core.fakes import model_hub_error

    if via_gateway:
        return model_hub_error(detail)
    return {"type": "error", "error": {"type": "invalid_request_error", "message": detail}}


def _searched_and_answered() -> str:
    """An Anthropic turn that thinks (signed), searches natively, and answers."""

    events = (
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "look it up"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-account-a"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": USE},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": RESULT},
        {"type": "content_block_stop", "index": 2},
        {"type": "content_block_start", "index": 3, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 3, "delta": {"type": "text_delta", "text": "v8 is out"}},
        {"type": "content_block_stop", "index": 3},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    return _sse("anthropic", events)


def _thinking_first_refusal(body: dict[str, Any]) -> Optional[str]:
    """Anthropic's refusal of a request that, with thinking on, continues a tool loop whose assistant message does not
    start with a thinking block; None when it holds."""

    thinking = body.get("thinking", {}).get("type")
    if thinking == "disabled" and "opus-5-5" in body.get("model", ""):
        return "thinking.type: Claude Opus 5.5 does not support `disabled`"
    # Only manual thinking requires the continued assistant message to start with thinking.
    if thinking != "enabled":
        return None
    messages = body["messages"]
    last = next((index for index in range(len(messages) - 1, -1, -1) if messages[index]["role"] == "assistant"), None)
    if last is None or not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for message in messages[last + 1 :]
        if isinstance(message["content"], list)
        for block in message["content"]
    ):
        return None
    first = messages[last]["content"][0]["type"]
    if first in ("thinking", "redacted_thinking"):
        return None
    return (
        f"messages.{last}.content.0.type: Expected `thinking` or `redacted_thinking`, but found `{first}`. When "
        "`thinking` is enabled, a final `assistant` message must start with a thinking block (preceeding the lastmost "
        "set of `tool_use` and `tool_result` blocks). We recommend you include thinking blocks from previous turns. To "
        "avoid this requirement, disable `thinking`."
    )


async def _session(
    answer: Any, endpoint: ModelEndpoint, turns: int, *, thinking: bool = False
) -> tuple[list[list[httpx.Request]], list[Any]]:
    """``turns`` turns of one Session (one store): the requests each turn sent, and how each ended. ``thinking`` runs
    the Agent with thinking on, and the gateway enforces Anthropic's rule for it."""

    sent: list[list[httpx.Request]] = []

    def gateway(wire: httpx.Request) -> httpx.Response:
        sent[-1].append(wire)
        refused = _thinking_first_refusal(json.loads(wire.content))
        if refused is not None:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": refused}})
        return answer(len(sent), len(sent[-1]))

    capabilities = (
        replace(CAPABILITIES, supports_reasoning=True, reasoning_efforts=("high",)) if thinking else CAPABILITIES
    )

    store = InMemoryTranscriptStore()
    ended = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        for turn in range(1, turns + 1):
            sent.append([])
            router = _Hops(client, (endpoint,), capabilities)
            agent = Agent(
                session_id="session", models=router, tools=(_hosted_tool("web_search"), READ), hooks=(), store=store,
                jobs=FakeJobHost(), cwd="/t", reasoning_effort="high" if thinking else None,
            )
            events = [event async for event in agent.run(input_row(f"input-{turn}", "and then?"), turn_id=f"turn-{turn}")]
            ended.append(events[-1])
    return sent, ended


@pytest.mark.parametrize("via_gateway", [True, False], ids=["through-model-hub", "upstream-body"])
@pytest.mark.parametrize(
    ("refusal", "demoted", "kept"),
    [(_SEARCH_REFUSAL, '"opaque"', "sig-account-a"), (_SIGNATURE_REFUSAL, "sig-account-a", '"opaque"')],
    ids=["search-result", "thinking-signature"],
)
async def test_an_opaque_payload_another_account_refuses_is_sent_portably_from_then_on_in_the_session(
    refusal, demoted, kept, via_gateway
):
    """A relay can serve a later turn from another upstream account, which cannot read the search results or thinking
    signatures the first minted; Anthropic then refuses the request before any output, naming what it refused. The
    Session's native search replays verbatim while that is accepted; once refused, the turn is sent again once without
    that kind of payload from that Source identity, in its portable form, and succeeds. The Session keeps the
    refusal: its next turn is portable from its first request, with no failed attempt. Only the kind refused goes."""

    def answer(turn: int, attempt: int) -> httpx.Response:
        if turn == 1:
            return httpx.Response(200, headers=_served("anthropic"), text=_searched_and_answered())
        if turn == 3 and attempt == 1:
            return httpx.Response(400, json=_anthropic_refusal(refusal, via_gateway=via_gateway))
        return httpx.Response(200, headers=_served("anthropic"), text=_sse("anthropic", _ANSWERED["anthropic"]))

    sent, ended = await _session(answer, _endpoint("anthropic", replay=True), turns=4)

    assert [event.reason for event in ended] == ["completed"] * 4
    bodies = [[wire.content.decode() for wire in turn] for turn in sent]
    # Accepted: the earlier turn's search and signed thinking replay verbatim.
    assert len(bodies[1]) == 1 and demoted in bodies[1][0] and kept in bodies[1][0]
    # Refused once, then sent again without what was refused, and nothing else lost.
    assert len(bodies[2]) == 2 and demoted in bodies[2][0]
    assert demoted not in bodies[2][1] and kept in bodies[2][1]
    if demoted == '"opaque"':
        # The search in its portable form: the query and the source, readable, with no encrypted data.
        assert "avibe release" in bodies[2][1] and "https://avibe.bot/r" in bodies[2][1]
    # The next turn: portable from its first request.
    assert len(bodies[3]) == 1 and demoted not in bodies[3][0] and kept in bodies[3][0]


async def test_a_refusal_of_what_the_session_already_demoted_stands_as_the_error():
    """A refusal is answered once: the same refusal again, after the payload it names was demoted, is the error."""

    def answer(turn: int, attempt: int) -> httpx.Response:
        if turn == 1:
            return httpx.Response(200, headers=_served("anthropic"), text=_searched_and_answered())
        return httpx.Response(400, json=_anthropic_refusal(_SEARCH_REFUSAL, via_gateway=True))

    sent, ended = await _session(answer, _endpoint("anthropic", replay=True), turns=2)

    assert len(sent[1]) == 2 and ended[1].reason == "error"


_THOUGHT = ThinkingBlock(text="plan the read", signature="sig-account-a")
_READ_CALL = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})


# Anthropic's rules for thinking in a tool loop (https://platform.claude.com/docs/en/build-with-claude/thinking):
# "In extended (manual) mode, the API additionally enforces that the final assistant turn of a thinking-enabled request
# begins with a thinking block. Adaptive mode relaxes this: no assistant turn needs to start with one." And Opus 5.5,
# Sonnet 5.5, Fable and Mythos "reject `thinking: {type: "disabled"}`".
_ADAPTIVE_MODEL = "claude-opus-5-5"
_MANUAL_MODEL = "claude-sonnet-4-5"
_NON_CLAUDE_MODEL = "grok-4.7"


@pytest.mark.parametrize(
    ("model", "continued", "after", "thinking"),
    [
        # Adaptive thinking never needs the continued message to start with thinking, and is never turned off.
        (_ADAPTIVE_MODEL, (ThinkingBlock(text="plan the read"), _READ_CALL), "result", "adaptive"),
        (_ADAPTIVE_MODEL, (_READ_CALL,), "result", "adaptive"),
        # Manual thinking: a loop whose signed thinking goes back keeps it on...
        (_MANUAL_MODEL, (_THOUGHT, _READ_CALL), "result", "enabled"),
        # ...one whose thinking went as text, or that starts with the call, continues with thinking off...
        (_MANUAL_MODEL, (ThinkingBlock(text="plan the read"), _READ_CALL), "result", "disabled"),
        (_MANUAL_MODEL, (_READ_CALL,), "result", "disabled"),
        # ...also when a user message follows the results: Anthropic combines consecutive user messages into one turn,
        # so the tool loop the rule names is still the last one...
        (_MANUAL_MODEL, (_READ_CALL,), "result-then-user", "disabled"),
        # ...while a new turn after an answer keeps it on.
        (_MANUAL_MODEL, (TextBlock("done"),), "user", "enabled"),
        # An Anthropic-compatible non-Claude model does not receive the Claude-specific fallback...
        (_NON_CLAUDE_MODEL, (_READ_CALL,), "result", None),
        # ...but its normal non-tool-loop reasoning request remains unchanged.
        (_NON_CLAUDE_MODEL, (TextBlock("done"),), "user", "enabled"),
    ],
    ids=["adaptive-thinking-as-text", "adaptive-call-first", "manual-signed-thinking-first", "manual-thinking-as-text",
         "manual-call-first", "manual-user-message-after-results", "manual-new-turn", "non-claude-tool-loop",
         "non-claude-new-turn"],
)
async def test_anthropic_thinking_fallback_is_scoped_to_manual_claude_tool_loops(
    model, continued, after, thinking
):
    """Manual Claude uses the #2736 fallback; adaptive Claude stays on; other models omit it rather than guessing."""

    endpoint = replace(_endpoint("anthropic", replay=False, native=False), hosted_tools={}, model_id=model)
    result = ToolResultMessage("toolu_1", "read", (TextBlock("contents"),))
    tail = {
        "result": (result,),
        "result-then-user": (result, UserMessage((TextBlock("and then?"),))),
        "user": (UserMessage((TextBlock("and then?"),)),),
    }[after]
    stop = "stop" if after == "user" else "tool_use"
    message = AssistantMessage(continued, Origin("anthropic", "anthropic", model), stop, source_identity=IDENTITY)
    request = replace(_request(endpoint, message, *tail), reasoning_effort="high")

    wire = await build_wire_request(request, gateway=True)

    if thinking is None:
        assert "thinking" not in wire.body
    else:
        assert wire.body["thinking"]["type"] == thinking


def _thought_and_read() -> str:
    """An Anthropic response that thinks (signed) and calls read."""

    events = (
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "read it first"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-account-b"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_read", "name": "read", "input": {"path": "a"}}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    return _sse("anthropic", events)


@pytest.mark.parametrize(
    ("model", "continuation"), [(_ADAPTIVE_MODEL, "adaptive"), (_MANUAL_MODEL, "disabled")], ids=["adaptive", "manual"]
)
async def test_a_session_whose_signatures_were_refused_still_finishes_tool_loops_with_thinking_on(
    monkeypatch, model, continuation
):
    """After the provider refused a thinking signature, the Session's thinking goes back as text, including thinking the
    turn minted a moment ago. Under manual thinking a tool loop it continues would break Anthropic's rule that the
    continued assistant message starts with a thinking block, so that request goes with thinking off; adaptive thinking
    has no such rule and keeps thinking on (Opus 5.5 refuses ``disabled``). The gateway here enforces both as
    Anthropic does, and the loop finishes either way."""

    monkeypatch.setitem(MODELS, "anthropic", ("anthropic", model))

    def answer(turn: int, attempt: int) -> httpx.Response:
        if turn == 1:
            return httpx.Response(200, headers=_served("anthropic"), text=_searched_and_answered())
        if attempt == 1:
            return httpx.Response(400, json=_anthropic_refusal(_SIGNATURE_REFUSAL, via_gateway=True))
        if attempt == 2:
            return httpx.Response(200, headers=_served("anthropic"), text=_thought_and_read())
        return httpx.Response(200, headers=_served("anthropic"), text=_sse("anthropic", _ANSWERED["anthropic"]))

    sent, ended = await _session(answer, _endpoint("anthropic", replay=True), turns=2, thinking=True)

    assert [event.reason for event in ended] == ["completed", "completed"]
    bodies = [json.loads(wire.content) for wire in sent[1]]
    # Refused, resent without signatures, then the loop's continuation with thinking off.
    assert len(bodies) == 3
    first = "adaptive" if model == _ADAPTIVE_MODEL else "enabled"
    assert [body["thinking"]["type"] for body in bodies] == [first, first, continuation]
