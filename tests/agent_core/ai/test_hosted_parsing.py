"""Hosted-call parsing (``vibey-hosted-tools.md`` §4.2), read from stub provider streams. Scenario MH-HOSTED-VIBEY-002.

Every item a provider emits for a hosted tool the request declared natively becomes exactly one ``HostedToolBlock``
in stream order, with a provider-neutral action, its per-call record and the provider's items verbatim. Citations
belong to the text that cites them; response-scoped facts go to ``hosted_summary``. The stream shapes are the
2026-10-13 spike's, redacted.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import core.agent_core.ai._common as common_module
from core.agent_core.ai._common import OutputBudgetExceeded, StreamAssembler
from core.agent_core.ai.hosted import anthropic_hosted_block, reported_counts, responses_hosted_block
from core.agent_core.ai.provider import Done, HostedToolRoute, ModelEndpoint, ModelRequest
from core.agent_core.ai.registry import create_adapter
from core.agent_core.cancel import CancelToken
from core.agent_core.messages import (
    AssistantMessage,
    HostedSummary,
    message_to_dict,
    Citation,
    HostedAction,
    HostedCost,
    HostedSource,
    HostedToolBlock,
    Origin,
    TextBlock,
    ToolCallBlock,
    UserMessage,
)
from core.agent_core.tools.hosted import hosted_tool_spec

SERVED = "src_served00001"
ORIGIN_X = Origin("upstream", "openai_responses", "model-x")
WEB, X = hosted_tool_spec("web_search"), hosted_tool_spec("x_search")
NATIVE = {
    "web_search": HostedToolRoute("native", None, False, declaration={"type": "web_search"}),
    "x_search": HostedToolRoute("native", None, False, declaration={"type": "x_search"}),
}


def _sse(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


async def _message(
    protocol: str,
    body: str,
    *,
    hosted: dict[str, HostedToolRoute] = NATIVE,
    tools=(WEB, X),
    served: bool = True,
) -> AssistantMessage:
    request = ModelRequest(
        endpoint=ModelEndpoint(
            protocol,  # type: ignore[arg-type]
            "http://hub.invalid/vibey/v1",
            "model-x",
            "hub-token",
            provider="upstream",
            source_id=SERVED,
            hosted_tools=hosted,
        ),
        system="system",
        messages=(UserMessage((TextBlock("search"),)),),
        tools=tools,
        max_tokens=256,
    )
    headers = {"content-type": "text/event-stream"}
    if served:
        headers["x-avibe-served-hop"] = json.dumps(
            {"provider": "upstream", "api": protocol, "model": "served-model", "source": SERVED}, separators=(",", ":")
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, text=body))
    ) as client:
        adapter = create_adapter(protocol, client=client, gateway=True)  # type: ignore[arg-type]
        events = [event async for event in adapter.stream(request, CancelToken())]
    assert isinstance(events[-1], Done), events[-1]
    return events[-1].message


# --- OpenAI Responses (OpenAI, xAI) ---------------------------------------------------------------------------

WEB_SEARCH_CALL = {
    "id": "ws_1",
    "type": "web_search_call",
    "status": "completed",
    "action": {
        "type": "search",
        "query": "CLIProxyAPI latest release",
        "sources": [{"type": "url", "url": "https://github.com/router-for-me/CLIProxyAPI/releases"}],
    },
}
ANSWER = "The latest release is v8.0.27.[[1]](https://github.com/router-for-me/CLIProxyAPI/releases)"
ANSWER_ITEM = {
    "id": "msg_1",
    "type": "message",
    "content": [
        {
            "type": "output_text",
            "text": ANSWER,
            "annotations": [
                {
                    "type": "url_citation",
                    "url": "https://github.com/router-for-me/CLIProxyAPI/releases",
                    "start_index": 29,
                    "end_index": len(ANSWER),
                    "title": "1",
                }
            ],
        }
    ],
}


def _responses(
    items: list[dict[str, Any]], usage: dict[str, Any], *, done_order: list[int] | None = None, repeat: bool = True
) -> str:
    """Every item added in output order, then done in ``done_order``; the terminal response repeats them unless
    ``repeat`` is false."""

    events: list[dict[str, Any]] = [{"type": "response.created", "response": {"status": "in_progress"}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index, "item": {**item, "status": "in_progress"}})
        if item["type"] == "web_search_call":
            # Progress frames are not model output and change nothing.
            events.append({"type": "response.web_search_call.searching", "output_index": index, "item_id": item["id"]})
        if item["type"] == "message":
            events.append({"type": "response.output_text.delta", "output_index": index, "delta": item["content"][0]["text"]})
    for index in done_order if done_order is not None else range(len(items)):
        events.append({"type": "response.output_item.done", "output_index": index, "item": items[index]})
    # The terminal response repeats every item; none is parsed twice.
    output = items if repeat else None
    events.append({"type": "response.completed", "response": {"status": "completed", "output": output, "usage": usage}})
    return _sse(*events)


async def test_an_xai_web_search_is_one_hosted_call_with_its_sources_cited_text_and_reported_cost():
    """MH-HOSTED-VIBEY-002: one hosted call per item, its sources per call, the citation on the text that cites it,
    and the response's counters and reported cost once in its summary; the terminal copy is never parsed twice."""
    usage = {
        "input_tokens": 22965,
        "output_tokens": 1402,
        "cost_in_usd_ticks": 277316400,
        "server_side_tool_usage_details": {"web_search_calls": 1, "x_search_calls": 0},
    }

    message = await _message("openai_responses", _responses([WEB_SEARCH_CALL, ANSWER_ITEM], usage))

    call, answer = message.content
    assert call == HostedToolBlock(
        id="ws_1",
        tool="web_search",
        action=HostedAction("search", queries=("CLIProxyAPI latest release",)),
        record=call.record,
        native=call.native,
    )
    assert call.record.status == "completed"
    assert call.record.sources == (HostedSource(url="https://github.com/router-for-me/CLIProxyAPI/releases"),)
    assert call.native.source_id == SERVED and call.native.payload == (WEB_SEARCH_CALL,)
    # xAI titles a citation with its number, which is no title.
    assert answer == TextBlock(
        text=ANSWER,
        citations=(Citation(url="https://github.com/router-for-me/CLIProxyAPI/releases", start=29, end=len(ANSWER)),),
    )
    summary = message.hosted_summary
    assert summary.counts == {"web_search_calls": 1}
    assert summary.cost.basis == "reported" and summary.cost.usd == pytest.approx(0.02773164)
    assert summary.cited_sources == ()
    assert message.stop_reason == "stop"


@pytest.mark.parametrize("repeat", [True, False], ids=["terminal-output", "no-terminal-output"])
async def test_xai_x_searches_keep_their_variants_and_the_answer_cites_each_post_once(repeat):
    calls = [
        {
            "type": "custom_tool_call",
            "id": f"ctc_{n}",
            "call_id": f"xs_call-{n}",
            "name": name,
            "input": json.dumps(arguments),
            "status": "completed",
        }
        for n, (name, arguments) in enumerate(
            [
                ("x_keyword_search", {"query": "from:OpenAI within_time:7d", "limit": "10", "mode": "Latest"}),
                ("x_semantic_search", {"query": "GPT-6 launch"}),
            ]
        )
    ]
    post, other_post = "https://x.com/OpenAI/status/1", "https://twitter.com/OpenAI/status/2"
    text = "[[1]](post1)[[2]](post2) OpenAI posted. [[1]](post1)"
    message_item = {
        "id": "msg_1",
        "type": "message",
        "content": [
            {
                "type": "output_text",
                "text": text,
                "annotations": [
                    {"type": "url_citation", "url": post, "start_index": 0, "end_index": 12, "title": "1"},
                    {"type": "url_citation", "url": other_post, "start_index": 12, "end_index": 24, "title": "2"},
                    {"type": "url_citation", "url": post, "start_index": 40, "end_index": 52, "title": "1"},
                ],
            }
        ],
    }
    # xAI's encrypted X-search output rides in a ``tco_`` reasoning item, which stays thinking.
    reasoning = {"type": "reasoning", "id": "tco_1", "summary": [], "encrypted_content": "opaque"}
    usage = {
        "input_tokens": 14987,
        "output_tokens": 2907,
        "cost_in_usd_ticks": 1127268800,
        "server_side_tool_usage_details": {"x_search_calls": 2, "x_posts_fetched": 20, "web_search_calls": 0},
    }

    message = await _message("openai_responses", _responses([*calls, reasoning, message_item], usage, repeat=repeat))

    keyword, semantic = (block for block in message.content if isinstance(block, HostedToolBlock))
    assert keyword.action == HostedAction(
        "x_search", variant="keyword", query="from:OpenAI within_time:7d", limit=10, mode="Latest"
    )
    assert semantic.action == HostedAction("x_search", variant="semantic", query="GPT-6 launch")
    assert keyword.record.sources == () and keyword.record.status == "completed"
    assert not any(isinstance(block, ToolCallBlock) for block in message.content)
    summary = message.hosted_summary
    assert summary.counts == {"x_search_calls": 2, "x_posts_fetched": 20}
    assert summary.cited_sources == (HostedSource(url=post), HostedSource(url=other_post))
    assert summary.cost.usd == pytest.approx(0.11272688)


@pytest.mark.parametrize(
    ("item", "action", "status"),
    [
        # ``queries`` wins over the deprecated singular ``query``.
        (
            {"type": "web_search_call", "status": "completed", "action": {"type": "search", "queries": ["a", "b"], "query": "a"}},
            HostedAction("search", queries=("a", "b")),
            "completed",
        ),
        (
            {"type": "web_search_call", "status": "completed", "action": {"type": "open_page", "url": "https://a.test"}},
            HostedAction("open_page", url="https://a.test"),
            "completed",
        ),
        (
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "find_in_page", "url": "https://a.test", "pattern": "v8"},
            },
            HostedAction("find_in_page", url="https://a.test", pattern="v8"),
            "completed",
        ),
        # An action this design does not know is kept as ``other``, never dropped.
        (
            {"type": "web_search_call", "status": "completed", "action": {"type": "scroll"}},
            HostedAction("other", name="scroll"),
            "completed",
        ),
        (
            {"type": "custom_tool_call", "name": "x_user_search", "input": '{"query": "OpenAI"}', "status": "completed"},
            HostedAction("x_user_search", query="OpenAI"),
            "completed",
        ),
        (
            {"type": "custom_tool_call", "name": "x_thread_fetch", "input": '{"post_id": "12"}', "status": "completed"},
            HostedAction("x_thread_fetch", ref="12"),
            "completed",
        ),
        (
            {"type": "custom_tool_call", "name": "x_media_search", "input": '{"query": "cats"}', "status": "completed"},
            HostedAction("other", name="x_media_search"),
            "completed",
        ),
        (
            {"type": "web_search_call", "status": "failed", "action": {"type": "search", "query": "a"}},
            HostedAction("search", queries=("a",)),
            "failed",
        ),
    ],
    ids=["queries", "open_page", "find_in_page", "unknown-action", "x_user_search", "x_thread_fetch", "unknown-x", "failed"],
)
def test_the_mapping_rule_gives_every_responses_item_a_provider_neutral_action(item, action, status):
    block = responses_hosted_block({"id": "item_1", **item}, source_id=SERVED, call_id="hosted_0")

    assert (block.action, block.record.status) == (action, status)
    assert (block.record.error is not None) == (status == "failed")
    assert block.native.payload == ({"id": "item_1", **item},)


@pytest.mark.parametrize(("hosted", "served"), [({}, True), (NATIVE, False)], ids=["not-declared", "unknown-source"])
async def test_no_hosted_call_without_a_native_declaration_or_a_known_serving_source(hosted, served):
    """A hosted item for a tool the request did not declare natively is not a hosted call of this request, and a
    response whose serving Source is unknown keeps none: its payload could never be replayed."""

    message = await _message(
        "openai_responses", _responses([WEB_SEARCH_CALL, ANSWER_ITEM], {"input_tokens": 1}), hosted=hosted, served=served
    )

    assert [type(block) for block in message.content] == [TextBlock]
    assert message.hosted_summary is None


# --- Anthropic Messages -------------------------------------------------------------------------------------


def _anthropic(*blocks: list[dict[str, Any]], stop_reason: str = "end_turn", server_tool_use: int = 1) -> str:
    events: list[dict[str, Any]] = [{"type": "message_start", "message": {"usage": {"input_tokens": 6}}}]
    for index, frames in enumerate(blocks):
        events.extend({**frame, "index": index} for frame in frames)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": 519, "server_tool_use": {"web_search_requests": server_tool_use, "web_fetch_requests": 0}},
        }
    )
    events.append({"type": "message_stop"})
    return _sse(*events)


def _use(call_id: str, query: str) -> list[dict[str, Any]]:
    half = len(query) // 2
    return [
        {"type": "content_block_start", "content_block": {"type": "server_tool_use", "id": call_id, "name": "web_search", "input": {}}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": f'{{"query": "{query[:half]}'}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": f'{query[half:]}"}}'}},
    ]


def _result(call_id: str, content: Any) -> list[dict[str, Any]]:
    return [{"type": "content_block_start", "content_block": {"type": "web_search_tool_result", "tool_use_id": call_id, "content": content}}]


HIT = {
    "type": "web_search_result",
    "title": "Releases · router-for-me/CLIProxyAPI",
    "url": "https://github.com/router-for-me/CLIProxyAPI/releases",
    "encrypted_content": "opaque",
    "page_age": "15 days ago",
}
LOCATION = {
    "type": "web_search_result_location",
    "url": HIT["url"],
    "title": HIT["title"],
    "cited_text": "v8.0.24 Latest",
    "encrypted_index": "opaque",
}


async def test_an_anthropic_search_and_its_result_are_one_hosted_call_and_the_text_keeps_its_citation():
    text = [
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "The latest release is v8.0.24."}},
        {"type": "content_block_delta", "delta": {"type": "citations_delta", "citation": LOCATION}},
    ]

    message = await _message("anthropic", _anthropic(_use("srvtoolu_1", "CLIProxyAPI releases"), _result("srvtoolu_1", [HIT]), text), tools=(WEB,))

    call, answer = message.content
    assert call.action == HostedAction("search", queries=("CLIProxyAPI releases",))
    assert call.record.status == "completed"
    assert call.record.sources == (HostedSource(url=HIT["url"], title=HIT["title"], page_age="15 days ago"),)
    use = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "CLIProxyAPI releases"}}
    result = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [HIT]}
    assert call.native.payload == (use, result)
    # An Anthropic citation covers its whole text block, so it carries no range.
    assert answer == TextBlock(
        text="The latest release is v8.0.24.",
        citations=(Citation(url=HIT["url"], title=HIT["title"], cited_text="v8.0.24 Latest"),),
    )
    assert message.hosted_summary.counts == {"web_search_requests": 1}
    assert message.hosted_summary.cost is None  # unknown is never priced as zero


async def test_parallel_anthropic_searches_finalize_by_id_and_a_search_cut_off_stays_pending():
    client_call = [
        {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"path": "a"}'}},
    ]
    failed = {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}

    message = await _message(
        "anthropic",
        _anthropic(
            _use("srvtoolu_a", "first"),
            _use("srvtoolu_b", "second"),
            _use("srvtoolu_c", "third"),
            _result("srvtoolu_b", [HIT]),
            _result("srvtoolu_a", failed),
            client_call,
            stop_reason="tool_use",
            server_tool_use=2,
        ),
        tools=(WEB,),
    )

    first, second, third, read = message.content
    assert [first.id, second.id, third.id] == ["srvtoolu_a", "srvtoolu_b", "srvtoolu_c"]
    assert (first.record.status, first.record.error, first.record.error_code) == ("failed", "max_uses_exceeded", None)
    assert second.record.status == "completed"
    assert third.record.status == "pending" and len(third.native.payload) == 1
    assert read == ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"}, native_id="toolu_1")
    assert message.stop_reason == "tool_use"


async def test_parallel_responses_calls_keep_their_output_order_whatever_order_they_finish_in():
    second = {**WEB_SEARCH_CALL, "id": "ws_2", "action": {"type": "search", "query": "second"}}

    message = await _message(
        "openai_responses", _responses([WEB_SEARCH_CALL, second, ANSWER_ITEM], {"input_tokens": 1}, done_order=[1, 0, 2])
    )

    assert [getattr(block, "id", "text") for block in message.content] == ["ws_1", "ws_2", "text"]
    assert all(block.record.status == "completed" for block in message.content[:2])


_SEARCH_USE = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "a"}}


@pytest.mark.parametrize(
    ("block", "reason", "code"),
    [
        (
            lambda: responses_hosted_block(
                {"id": "ws_1", "type": "web_search_call", "status": "failed", "error": {"message": "upstream timeout"}},
                source_id=SERVED,
                call_id="hosted_0",
            ),
            "upstream timeout",
            None,
        ),
        (
            lambda: responses_hosted_block(
                {"id": "ws_1", "type": "web_search_call", "status": "incomplete"}, source_id=SERVED, call_id="hosted_0"
            ),
            "the provider ended the call incomplete",
            "provider_no_reason",
        ),
        (
            lambda: anthropic_hosted_block(
                _SEARCH_USE,
                {"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error"}},
                source_id=SERVED,
            ),
            "the provider reported no reason",
            "provider_no_reason",
        ),
    ],
    ids=["responses-reason", "responses-no-reason", "anthropic-no-reason"],
)
def test_a_failed_call_keeps_the_providers_reason_uncoded_and_codes_the_one_avibe_writes_for_none(block, reason, code):
    """#2589: a display localizes a hosted failure by its ``error_code``, and only Avibe's own English reasons have one;
    the provider's words show as they are."""

    record = block().record
    assert (record.error, record.error_code) == (reason, code)


def test_a_hosted_error_is_redacted_before_the_record_keeps_it():
    """The record is persisted and replayed to later models, so a provider's echoed credential never reaches it."""

    secret = "sk-proj-" + "a1B2c3D4e5F6" * 4
    item = {"id": "ws_1", "type": "web_search_call", "status": "failed", "error": {"message": f"bad key {secret}"}}

    block = responses_hosted_block(item, source_id=SERVED, call_id="hosted_0")

    assert block.record.status == "failed" and secret not in block.record.error


@pytest.mark.parametrize(
    "retain",
    [
        lambda assembler: assembler.hosted_input_delta(0, "q" * 16384),
        lambda assembler: assembler.add_text_citation(0, Citation(url="https://a.test", cited_text="c" * 16384)),
        lambda assembler: assembler.hosted_block(
            0, responses_hosted_block({**WEB_SEARCH_CALL, "padding": "p" * 16384}, source_id=SERVED, call_id="h")
        ),
        # The payload alone fits; its canonical copies in the action and record do not.
        lambda assembler: assembler.hosted_block(
            0,
            responses_hosted_block(
                {
                    **WEB_SEARCH_CALL,
                    "action": {"type": "search", "query": "q" * 2500, "sources": [{"type": "url", "url": "https://" + "u" * 2500}]},
                },
                source_id=SERVED,
                call_id="h",
            ),
        ),
    ],
    ids=["streamed-input", "citation", "payload", "canonical-copies"],
)
def test_every_byte_a_hosted_call_or_citation_keeps_counts_against_the_output_budget(monkeypatch, retain):
    monkeypatch.setattr(common_module, "MAX_CUMULATIVE_OUTPUT_CHARS", 8192)
    assembler = StreamAssembler(origin=Origin("upstream", "anthropic", "model-x"), protocol="anthropic")

    with pytest.raises(OutputBudgetExceeded):
        retain(assembler)


def test_the_requests_own_credentials_are_in_no_string_a_hosted_call_keeps():
    """A provider can echo a configured key no generic pattern recognises, anywhere in its item. The assembler's one
    boundary removes it as an exact literal from everything the call keeps, its native payload included, and leaves
    every other byte of the payload as the provider sent it."""

    assembler = StreamAssembler(
        origin=Origin("upstream", "openai_responses", "model-x"),
        protocol="openai_responses",
        sensitive_values=("relay-key-7",),
    )
    item = {
        "id": "ws_1",
        "type": "web_search_call",
        "status": "failed",
        "error": {"message": "key relay-key-7 refused"},
        "action": {"type": "search", "query": "why relay-key-7", "sources": [{"type": "url", "url": "https://a.test/?k=relay-key-7"}]},
        "encrypted": "sk-0123456789abcdefABCDEF0123456789",
        "echo": {"relay-key-7": "as a key"},
    }

    assembler.hosted_block(0, responses_hosted_block(item, source_id=SERVED, call_id="h"))

    kept = assembler.content[0]
    serialized = json.dumps(message_to_dict(AssistantMessage((kept,), ORIGIN_X, "stop", hosted_summary=HostedSummary())))
    assert "relay-key-7" not in serialized
    # No pattern rewrote the opaque provider string a replay must send back exactly.
    assert kept.native.payload[0]["encrypted"] == item["encrypted"]


def test_hosted_facts_are_charged_as_kept_and_a_summary_keeps_at_most_32_counters(monkeypatch):
    assembler = StreamAssembler(origin=ORIGIN_X, protocol="openai_responses")
    assembler.set_origin(ORIGIN_X, True, SERVED)
    assembler.hosted_block(0, responses_hosted_block(WEB_SEARCH_CALL, source_id=SERVED, call_id="h"))

    assembler.set_hosted_facts(counts={f"counter_{n}": 1 for n in range(100)})

    assert len(assembler.finalize("stop").hosted_summary.counts) == 32
    monkeypatch.setattr(common_module, "MAX_CUMULATIVE_OUTPUT_CHARS", 8192)
    fresh = StreamAssembler(origin=ORIGIN_X, protocol="openai_responses")
    # About 5,000 characters, but over 12,000 UTF-8 bytes: the charge is in bytes.
    with pytest.raises(OutputBudgetExceeded):
        fresh.set_hosted_facts(cited=(HostedSource(url="https://x.com/" + "é" * 3000, title="中" * 2000),))


def test_a_usage_section_of_many_counters_is_read_only_up_to_the_cap():
    counters = {f"counter_{n}": 1 for n in range(10_000)}

    assert reported_counts(counters) == {f"counter_{n}": 1 for n in range(32)}
