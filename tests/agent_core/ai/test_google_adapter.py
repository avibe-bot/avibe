"""The Gemini adapter at its boundary: the request it sends, and the transcript a recorded stream becomes."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from core.agent_core.ai.google import DUMMY_THOUGHT_SIGNATURE, GoogleAdapter
from core.agent_core.ai.registry import sent_effort
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelEndpoint, ModelRequest, ProviderError
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    Citation,
    HostedSource,
    HostedToolBlock,
    ImageBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from core.agent_core.tools.base import ToolSpec
from tests.agent_core.ai._wire import build_wire_request

SIG_A = base64.b64encode(b"signature-a").decode()
SIG_B = base64.b64encode(b"signature-b!").decode()
MODEL = "gemini-4-argon"
BASH = ToolSpec("bash", "Run a command", {"type": "object", "properties": {"command": {"type": "string"}}})
GEMINI = Origin("google", "google", MODEL)


def _request(
    *messages: Any, model: str = MODEL, effort: str | None = None, base_url: str = "https://gemini.invalid/v1beta",
    tools: tuple[ToolSpec, ...] = (BASH,), supports_images: bool = False,
) -> ModelRequest:
    return ModelRequest(
        endpoint=ModelEndpoint("google", base_url, model, "key", provider="google"),
        system="system",
        messages=messages or (UserMessage((TextBlock(text="hello"),)),),
        tools=tools,
        max_tokens=256,
        reasoning_effort=effort,
        supports_images=supports_images,
    )


def _user(text: str) -> UserMessage:
    return UserMessage((TextBlock(text=text),))


def _calls(*calls: ToolCallBlock, origin: Origin = GEMINI) -> AssistantMessage:
    return AssistantMessage(content=calls, origin=origin, stop_reason="tool_use")


def _result(call_id: str, text: str, *, images: tuple[ImageBlock, ...] = ()) -> ToolResultMessage:
    return ToolResultMessage(tool_call_id=call_id, tool_name="bash", content=(TextBlock(text=text), *images))


async def _body(request: ModelRequest, *, gateway: bool = False) -> dict[str, Any]:
    return (await build_wire_request(request, gateway=gateway)).body


async def _stream(*chunks: dict[str, Any], request: ModelRequest | None = None) -> list[Any]:
    seen: list[httpx.Request] = []

    def handler(sent: httpx.Request) -> httpx.Response:
        seen.append(sent)
        body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [event async for event in GoogleAdapter(client).stream(request or _request(), CancelToken())]
    _stream.sent = seen  # type: ignore[attr-defined]
    return events


def _final(events: list[Any]) -> AssistantMessage:
    terminal = events[-1]
    assert isinstance(terminal, Done), terminal
    return terminal.message


def _parts(*parts: dict[str, Any], finish: str | None = None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}}
    if finish:
        candidate["finishReason"] = finish
    chunk: dict[str, Any] = {"candidates": [candidate]}
    if usage:
        chunk["usageMetadata"] = usage
    return chunk


async def test_the_request_is_native_gemini_at_the_stream_endpoint() -> None:
    request = _request(_user("hi"))
    direct = await build_wire_request(request, gateway=False)
    through_gateway = await build_wire_request(request, gateway=True)

    assert direct.url == "https://gemini.invalid/v1beta/models/gemini-4-argon:streamGenerateContent?alt=sse"
    # Google takes its key in its own header; the gateway takes the gateway token as a bearer.
    assert direct.headers["x-goog-api-key"] == "key" and "Authorization" not in direct.headers
    assert through_gateway.headers["Authorization"] == "Bearer key"
    # The gateway refuses a Gemini body that names its model or stream mode: both live in the URL.
    assert direct.body == {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {"parts": [{"text": "system"}]},
        "tools": [{"functionDeclarations": [
            {"name": "bash", "description": "Run a command", "parametersJsonSchema": dict(BASH.input_schema)},
        ]}],
        "generationConfig": {"maxOutputTokens": 256, "thinkingConfig": {"includeThoughts": True}},
    }


@pytest.mark.parametrize(
    ("model", "effort", "thinking"),
    [
        ("gemini-4-argon", "high", {"includeThoughts": True, "thinkingLevel": "high"}),
        ("gemini-4-argon", "xhigh", {"includeThoughts": True, "thinkingLevel": "high"}),
        # Pro-class models refuse MINIMAL (measured on gemini-4-argon); Google's own Chat layer sends LOW.
        ("gemini-4-argon", "minimal", {"includeThoughts": True, "thinkingLevel": "low"}),
        ("gemini-3-flash-preview", "minimal", {"includeThoughts": True, "thinkingLevel": "minimal"}),
        ("gemini-flash-latest", "none", {"includeThoughts": True, "thinkingLevel": "minimal"}),
        ("gemini-2.5-flash", "medium", {"includeThoughts": True, "thinkingBudget": 8192}),
        ("gemini-2.5-flash", "none", {"includeThoughts": False, "thinkingBudget": 0}),
        ("gemini-2.5-pro", None, {"includeThoughts": True}),
        ("gemma-4-31b-it", "high", None),
    ],
)
async def test_the_effort_becomes_the_models_thinking_control(model: str, effort: str | None, thinking: Any) -> None:
    body = await _body(_request(model=model, effort=effort))
    assert body["generationConfig"].get("thinkingConfig") == thinking
    # Model Hub keys effort evidence by the token the registry names, and its gateway reads that same token back from
    # the request, so a refusal governs every effort that sends it (`minimal` and `low` both send LOW to a Pro model).
    from core.handlers.model_hub.evidence_rules import wire_effort

    assert sent_effort("google", model, effort or "") == wire_effort(body, "google")


async def test_signatures_go_back_on_the_parts_they_came_on_and_only_as_base64() -> None:
    served = AssistantMessage(
        content=(
            ThinkingBlock(text="plan", signature=SIG_A),
            TextBlock(text="checking"),
            ToolCallBlock(id="call_1", name="bash", arguments={"command": "ls"}, signature=SIG_B),
            ToolCallBlock(id="call_2", name="bash", arguments={"command": "pwd"}),
        ),
        origin=GEMINI,
        stop_reason="tool_use",
    )
    over_chat = AssistantMessage(
        # A response a google hop served over Chat before: its thinking marker is no Gemini signature.
        content=(ThinkingBlock(text="old", signature="reasoning_content"), TextBlock(text="earlier"),
                 ThinkingBlock(text="", signature=SIG_A)),
        origin=GEMINI,
        stop_reason="stop",
    )
    body = await _body(_request(
        _user("before"), over_chat, _user("now"), served, _result("call_1", "a"), _result("call_2", "b"),
    ))

    earlier, current, results = body["contents"][1], body["contents"][3], body["contents"][4]
    # An answer's signature, kept beside its text, goes back on that text part.
    assert earlier == {"role": "model", "parts": [
        {"text": "old", "thought": True}, {"text": "earlier", "thoughtSignature": SIG_A},
    ]}
    assert current == {"role": "model", "parts": [
        {"text": "plan", "thought": True, "thoughtSignature": SIG_A},
        {"text": "checking"},
        {"functionCall": {"name": "bash", "args": {"command": "ls"}, "id": "call_1"}, "thoughtSignature": SIG_B},
        # A parallel call carries no signature of its own; Gemini signs only the first.
        {"functionCall": {"name": "bash", "args": {"command": "pwd"}, "id": "call_2"}},
    ]}
    # One user content holds the step's results, in call order.
    assert results == {"role": "user", "parts": [
        {"functionResponse": {"name": "bash", "id": "call_1", "response": {"output": "a"}}},
        {"functionResponse": {"name": "bash", "id": "call_2", "response": {"output": "b"}}},
    ]}


async def test_another_origins_call_in_the_current_turn_carries_googles_stand_in_signature() -> None:
    claude = Origin("anthropic", "anthropic", "claude-x")
    earlier = _calls(ToolCallBlock(id="toolu_1", name="bash", arguments={}), origin=claude)
    current = _calls(
        ToolCallBlock(id="toolu_2", name="bash", arguments={}), ToolCallBlock(id="toolu_3", name="bash", arguments={}),
        origin=claude,
    )
    body = await _body(_request(
        _user("first"), earlier, _result("toolu_1", "x"),
        _user("second"), current, _result("toolu_2", "y"), _result("toolu_3", "z"),
    ))

    contents = body["contents"]
    # Gemini validates the first call of each step of the current turn only, so only that one needs it.
    assert "thoughtSignature" not in contents[1]["parts"][0]
    first, second = contents[4]["parts"]
    assert first["thoughtSignature"] == DUMMY_THOUGHT_SIGNATURE
    assert "thoughtSignature" not in second


@pytest.mark.parametrize(("model", "nested"), [("gemini-4-argon", True), ("gemini-2.5-flash", False)])
async def test_a_tool_results_image_rides_where_the_model_reads_it(model: str, nested: bool) -> None:
    image = ImageBlock(mime_type="image/png", media_token="tok", name="shot.png")
    loaded = {"tok": (b"\x89PNG", "image/png")}

    class Loader:
        async def load(self, media_token: str) -> tuple[bytes, str]:
            return loaded[media_token]

    calls = _calls(
        ToolCallBlock(id="call_1", name="bash", arguments={}, signature=SIG_A),
        ToolCallBlock(id="call_2", name="bash", arguments={}),
    )
    request = _request(
        _user("look"), calls, _result("call_1", "", images=(image,)), _result("call_2", "two"),
        model=model, supports_images=True,
    )
    body = (await build_wire_request(request, gateway=False, media_loader=Loader())).body

    # The parallel batch's results stay together, whichever of them carries the image.
    first, second = body["contents"][2]["parts"]
    assert first["functionResponse"]["response"] == {"output": "(see attached image)"}
    assert second["functionResponse"]["response"] == {"output": "two"}
    inline = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"\x89PNG").decode()}}
    if nested:
        assert first["functionResponse"]["parts"] == [inline] and len(body["contents"]) == 3
    else:
        assert "parts" not in first["functionResponse"]
        assert body["contents"][3:] == [{"role": "user", "parts": [{"text": "Tool result image:"}, inline]}]


async def test_a_stream_becomes_thinking_text_and_signed_calls() -> None:
    events = await _stream(
        _parts({"text": "Let me ", "thought": True}),
        _parts({"text": "check.", "thought": True, "thoughtSignature": SIG_A}),
        _parts({"text": "Running it."}),
        _parts(
            {"functionCall": {"name": "bash", "args": {"command": "ls"}, "id": "fc_1"}, "thoughtSignature": SIG_B},
            {"functionCall": {"name": "bash", "args": {"command": "pwd"}, "id": "fc_2"}},
            finish="STOP",
            usage={"promptTokenCount": 100, "cachedContentTokenCount": 40, "candidatesTokenCount": 20,
                   "thoughtsTokenCount": 7, "totalTokenCount": 127},
        ),
    )

    message = _final(events)
    assert message.stop_reason == "tool_use"
    assert message.content == (
        ThinkingBlock(text="Let me check.", signature=SIG_A),
        TextBlock(text="Running it."),
        ToolCallBlock(id="fc_1", name="bash", arguments={"command": "ls"}, native_id="fc_1", signature=SIG_B),
        ToolCallBlock(id="fc_2", name="bash", arguments={"command": "pwd"}, native_id="fc_2"),
    )
    # The prompt count includes the cache; thoughts are counted apart from the candidates.
    assert message.usage == Usage(input_tokens=60, output_tokens=27, cache_read_tokens=40, reasoning_tokens=7)


@pytest.mark.parametrize(
    "answer",
    [
        # Gemini streams the signature on an empty part after the text...
        ({"text": "all "}, {"text": "done"}, {"text": "", "thoughtSignature": SIG_A}),
        # ...or on a delta of the text itself, which goes on.
        ({"text": "all ", "thoughtSignature": SIG_A}, {"text": "done"}),
    ],
    ids=["empty-part", "on-text"],
)
async def test_an_answers_signature_goes_back_on_its_text(answer: tuple[dict[str, Any], ...]) -> None:
    events = await _stream(*(_parts(part) for part in answer), _parts(finish="STOP"))
    message = _final(events)
    assert message.content == (TextBlock(text="all done"), ThinkingBlock(text="", signature=SIG_A))

    body = await _body(_request(_user("go"), message, _user("again")))
    assert body["contents"][1] == {"role": "model", "parts": [{"text": "all done", "thoughtSignature": SIG_A}]}


async def test_a_signed_empty_part_after_a_call_goes_back_on_its_own() -> None:
    events = await _stream(
        _parts({"functionCall": {"name": "bash", "args": {}, "id": "fc_1"}, "thoughtSignature": SIG_B}),
        _parts({"text": "", "thoughtSignature": SIG_A}, finish="STOP"),
    )
    body = await _body(_request(_user("go"), _final(events), _result("fc_1", "ok")))
    assert body["contents"][1]["parts"][1] == {"text": "", "thoughtSignature": SIG_A}


@pytest.mark.parametrize(
    ("finish", "stop"),
    [("MAX_TOKENS", "length"), ("SAFETY", "safety"), ("RECITATION", "safety"), ("MALFORMED_FUNCTION_CALL", "error")],
)
async def test_a_finish_reason_maps_to_its_canonical_stop(finish: str, stop: str) -> None:
    message = _final(await _stream(_parts({"text": "partial"}, finish=finish)))
    assert message.stop_reason == stop
    assert (message.error_message is None) is (stop == "length")


async def test_a_blocked_prompt_is_a_safety_stop() -> None:
    message = _final(await _stream({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}))
    assert (message.stop_reason, message.content) == ("safety", ())
    assert "PROHIBITED_CONTENT" in (message.error_message or "")


async def test_a_stream_without_a_finish_reason_is_incomplete() -> None:
    error = (await _stream(_parts({"text": "cut"})))[-1]
    assert isinstance(error, ProviderError)
    assert (error.kind, error.retryable) == ("network", False)
    assert error.partial is not None and error.partial.content == (TextBlock(text="cut"),)


async def test_an_error_frame_is_classified_from_its_google_status() -> None:
    error = (await _stream({"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}}))[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "rate_limit" and error.retryable


# --- Google Search, Gemini's native web_search (vibey-gemini-native.md §4) ----------------------------------------

SOURCE = "src_gemini0001"
SEARCH = ToolSpec("web_search", "Search the web", {"type": "object", "properties": {"query": {"type": "string"}}})
NATIVE_SEARCH = HostedToolRoute(mode="native", replay=True, declaration={"type": "google_search"})
SERVED = json.dumps({"provider": "google", "api": "google", "model": MODEL, "source": SOURCE}, separators=(",", ":"))
CALL_PART = {
    "toolCall": {"toolType": "GOOGLE_SEARCH_WEB", "args": {"queries": ["btc price today", "btc close"]}, "id": "srch_1"},
    "thoughtSignature": SIG_A,
}
RESPONSE_PART = {
    "toolResponse": {"toolType": "GOOGLE_SEARCH_WEB", "response": {"search_suggestions": "<div>chips</div>"}, "id": "srch_1"},
    "thoughtSignature": SIG_B,
}
# Gemini lists only the queries that grounded the answer (measured: one of the call's two).
GROUNDING = {
    "webSearchQueries": ["btc price today"],
    "groundingChunks": [
        {"web": {"uri": "https://grounding.invalid/a", "title": "coindesk.com"}},
        {"web": {"uri": "https://grounding.invalid/b", "title": "coinbase.com"}},
        {"web": {"uri": "https://grounding.invalid/a", "title": "coindesk.com"}},
    ],
    "groundingSupports": [
        {"segment": {"startIndex": 0, "endIndex": 21, "text": "BTC closed at $79,6xx"}, "groundingChunkIndices": [0, 1]},
    ],
    "searchEntryPoint": {"renderedContent": "<div>chips</div>"},
}
A = "https://grounding.invalid/a"
B = "https://grounding.invalid/b"


def _searching(*messages: Any) -> ModelRequest:
    request = _request(*messages, base_url="http://hub.invalid/vibey/v1beta", tools=(BASH, SEARCH))
    return replace(request, endpoint=replace(
        request.endpoint, source_id=SOURCE, source_identity="sha256:gemini", hosted_tools={"web_search": NATIVE_SEARCH},
    ))


async def test_a_native_search_is_googles_own_tool_with_server_side_calls_enabled() -> None:
    wire = await build_wire_request(_searching(_user("btc?")), gateway=True)

    # Gemini runs Google Search next to functions only when the request says it will circulate their calls.
    assert wire.body["tools"] == [
        {"functionDeclarations": [
            {"name": "bash", "description": "Run a command", "parametersJsonSchema": dict(BASH.input_schema)},
        ]},
        {"googleSearch": {}},
    ]
    assert wire.body["toolConfig"] == {"includeServerSideToolInvocations": True}
    # A native declaration only ever goes to the hop's own Source.
    assert wire.headers["x-avibe-pin-source"] == SOURCE


async def test_a_google_search_is_one_hosted_call_whose_parts_go_back_verbatim() -> None:
    chunks = (
        _parts(CALL_PART),
        _parts(RESPONSE_PART),
        _parts({"text": "BTC closed at $79,6xx."}),
        # Gemini reports the response's searches and sources once, with its finish.
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "", "thoughtSignature": SIG_A}]},
                         "finishReason": "STOP", "groundingMetadata": GROUNDING}]},
    )

    def gateway(sent: httpx.Request) -> httpx.Response:
        body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-avibe-served-hop": SERVED}, text=body,
        )

    request = _searching(_user("btc?"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        events = [event async for event in GoogleAdapter(client, gateway=True).stream(request, CancelToken())]
    message = _final(events)

    [search] = [block for block in message.content if isinstance(block, HostedToolBlock)]
    assert (search.id, search.tool, search.action.type, search.action.queries) == (
        "srch_1", "web_search", "search", ("btc price today", "btc close"),
    )
    assert search.record.status == "completed"
    # The grounding chunks, each once; Google's Search Suggestions markup is not part of the record.
    assert search.record.sources == (
        HostedSource(url="https://grounding.invalid/a", title="coindesk.com"),
        HostedSource(url="https://grounding.invalid/b", title="coinbase.com"),
    )
    assert search.native.source_id == SOURCE and search.native.payload == (CALL_PART, RESPONSE_PART)
    # Google bills each distinct query the call ran, not only those the grounding lists.
    assert message.hosted_summary is not None and message.hosted_summary.counts == {"web_search_requests": 2}
    # The grounded sentence cites its sources at its range; the Search Suggestions are not kept.
    [answer] = [block for block in message.content if isinstance(block, TextBlock)]
    assert answer.citations == (
        Citation(url=A, title="coindesk.com", start=0, end=21), Citation(url=B, title="coinbase.com", start=0, end=21),
    )
    assert "chips" not in json.dumps([str(block) for block in message.content if not isinstance(block, HostedToolBlock)])

    # Back to the same Source, the call's parts go verbatim, signatures included, before the answer.
    body = await _body(_searching(_user("btc?"), message, _user("and eth?")), gateway=True)
    assert body["contents"][1]["parts"] == [
        CALL_PART, RESPONSE_PART, {"text": "BTC closed at $79,6xx.", "thoughtSignature": SIG_A},
    ]

    # Any other Source gets the portable form: a synthetic web_search call and result, no Google parts.
    other = _searching(_user("btc?"), message, _user("and eth?"))
    other = replace(other, endpoint=replace(other.endpoint, source_id="src_other0001"))
    wire = json.dumps((await _body(other, gateway=True))["contents"])
    assert "toolCall" not in wire and "toolResponse" not in wire and SIG_B not in wire
    assert '"name": "web_search"' in wire


async def test_a_server_side_call_the_request_did_not_declare_stays_out_of_the_transcript() -> None:
    grounded = {"candidates": [{"content": {"role": "model", "parts": []}, "groundingMetadata": GROUNDING}]}
    message = _final(await _stream(
        _parts(CALL_PART), _parts(RESPONSE_PART), _parts({"text": "BTC closed at $79,6xx."}, finish="STOP"), grounded,
    ))
    assert message.content == (TextBlock(text="BTC closed at $79,6xx."),) and message.hosted_summary is None


async def test_the_answer_a_search_of_an_earlier_response_grounds_cites_it() -> None:
    """Gemini reports the sources with the answer they ground: after a search and a function call, that is the next
    response of the turn, which ran no search and so is not billed for one (measured 2026-10-27)."""

    grounding = {**GROUNDING, "groundingSupports": [
        *GROUNDING["groundingSupports"],
        # A support whose text is not in the answer as streamed cites its source once, without a range.
        {"segment": {"text": "not in the answer"}, "groundingChunkIndices": [1, 7]},
        {"segment": {"text": "also elsewhere"}, "groundingChunkIndices": [1]},
    ]}
    finish = {"candidates": [{"content": {"role": "model", "parts": [{"text": "."}]}, "finishReason": "STOP",
                              "groundingMetadata": grounding}]}
    events = await _stream(_parts({"text": "Checked: "}), _parts({"text": "BTC closed at $79,6xx"}), finish,
                           request=_searching(_user("btc?")))
    message = _final(events)

    assert message.hosted_summary is None
    assert not [block for block in message.content if isinstance(block, HostedToolBlock)]
    [answer] = message.content
    assert answer == TextBlock(text="Checked: BTC closed at $79,6xx.", citations=(
        Citation(url=A, title="coindesk.com", start=9, end=30), Citation(url=B, title="coinbase.com", start=9, end=30),
        Citation(url=B, title="coinbase.com"),
    ))
