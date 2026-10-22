"""Anthropic mixed parallel groups: append-only deferred completion (hosted-tools §4.3)."""

from dataclasses import replace
import json

import httpx
import pytest

from core.agent_core.ai._common import PIN_MODEL_HEADER, PIN_SOURCE_HEADER
from core.agent_core.ai.hosted import anthropic_hosted_block
from core.agent_core.ai.provider import Done
from core.agent_core.messages import (
    AssistantMessage, HostedSummary, HostedToolBlock, TextBlock, ToolCallBlock,
    ToolResultMessage, UserMessage, message_from_dict, message_to_dict,
)
from tests.agent_core.ai._wire import build_wire_request
from tests.agent_core.ai.test_hosted_pause_turn import (
    MODEL, SOURCE, RESULT, _request, _response, _served, _stream, _text,
)

USE = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "avibe release"}}
CLIENT = {"type": "tool_use", "id": "toolu_client", "name": "read", "input": {"path": "a"}}
OTHER = "src_other000001"
SHAPES = ["SC", "CS", "SSC", "FSSCC"]


def _blocks(shape):
    blocks = []
    for index, kind in enumerate(shape):
        if kind == "C":
            blocks.append({**CLIENT, "id": f"toolu_{index}"})
        else:
            use = {**USE, "id": f"srvtoolu_{index}"}
            blocks.append(use)
            if kind == "F":
                blocks.append({**RESULT, "tool_use_id": use["id"]})
    return blocks


def _sse(blocks, *, stop="tool_use", searches=0):
    return _response(*[[{"type": "content_block_start", "content_block": block}] for block in blocks],
                     stop_reason=stop, input_tokens=100, output_tokens=10, searches=searches)


async def _paused(shape="SC"):
    request = _request()
    _, events = await _stream(lambda _: httpx.Response(200, headers=_served(), text=_sse(_blocks(shape))), request)
    assert isinstance(events[-1], Done)
    message = events[-1].message
    results = tuple(ToolResultMessage(call.id, call.name, (TextBlock("contents"),)) for call in message.tool_calls)
    return replace(request, messages=(*request.messages, message, *results)), message


@pytest.mark.parametrize("shape", SHAPES)
async def test_mixed_pause_preserves_exact_assistant_and_result_only_user_group(shape):
    request, message = await _paused(shape)
    first = await build_wire_request(_request(), gateway=True)
    wire = await build_wire_request(request, gateway=True)
    assert wire.headers[PIN_SOURCE_HEADER] == SOURCE
    assert wire.headers[PIN_MODEL_HEADER] == MODEL
    assert wire.body["tools"] == first.body["tools"]
    assert wire.body["messages"][1] == {"role": "assistant", "content": _blocks(shape)}
    users = wire.body["messages"][2:]
    assert len(users) == 1
    assert users[0]["role"] == "user"
    assert [item["tool_use_id"] for item in users[0]["content"]] == [call.id for call in message.tool_calls]
    assert {item["type"] for item in users[0]["content"]} == {"tool_result"}
    assert "interrupted" not in json.dumps(wire.body)


@pytest.mark.parametrize("shape", SHAPES)
async def test_orphan_result_finishes_prior_call_without_repeating_use_and_keeps_usage_here(shape):
    request, paused = await _paused(shape)
    pending = [b for b in paused.content if isinstance(b, HostedToolBlock) and b.record.status == "pending"]
    result_items = [{**RESULT, "tool_use_id": b.id} for b in pending]
    _, events = await _stream(lambda _: httpx.Response(200, headers=_served(),
        text=_sse(result_items + [{"type": "text", "text": "found"}], stop="end_turn", searches=len(pending))), request)
    assert isinstance(events[-1], Done)
    completed = events[-1].message
    calls = [b for b in completed.content if isinstance(b, HostedToolBlock)]
    assert [b.id for b in calls] == [b.id for b in pending]
    for before, after, item in zip(pending, calls, result_items):
        assert after.action == before.action and after.record.status == "completed"
        assert after.native.payload == (item,)
        assert [source.url for source in after.record.sources] == ["https://avibe.bot/r"]
    assert not paused.hosted_summary.counts
    assert completed.hosted_summary.counts == {"web_search_requests": len(pending)}
    assert message_from_dict(message_to_dict(completed)) == completed


async def test_a_result_from_another_source_cannot_complete_the_previous_sources_call():
    request, _ = await _paused()
    request = replace(request, endpoint=replace(request.endpoint, source_id=OTHER))
    result = {**RESULT, "tool_use_id": "srvtoolu_0"}
    sent, events = await _stream(lambda _: httpx.Response(200, headers=_served(source=OTHER),
        text=_sse([result, {"type": "text", "text": "other"}], stop="end_turn", searches=1)), request)
    assert not any(isinstance(b, HostedToolBlock) for b in events[-1].message.content)
    assert "server_tool_use" not in json.dumps(json.loads(sent[0].content)["messages"])


@pytest.mark.parametrize("reported", [0, 1])
async def test_deferred_completion_never_invents_a_charge_from_a_result_block(reported):
    request, _ = await _paused()
    result = {**RESULT, "tool_use_id": "srvtoolu_0"}
    _, events = await _stream(lambda _: httpx.Response(200, headers=_served(),
        text=_sse([result, {"type": "text", "text": "done"}], stop="end_turn", searches=reported)), request)
    message = events[-1].message
    assert message.hosted_summary.counts == ({"web_search_requests": 1} if reported else {})
    assert message.hosted_summary.cost is None


async def test_deferred_result_then_pause_turn_keeps_the_native_exchange_without_replay_evidence():
    request, _ = await _paused()
    result = {**RESULT, "tool_use_id": "srvtoolu_0"}
    def answer(number):
        blocks = [result] if number == 1 else [{"type": "text", "text": "done"}]
        return httpx.Response(200, headers=_served(), text=_sse(blocks,
            stop="pause_turn" if number == 1 else "end_turn", searches=1 if number == 1 else 0))
    sent, events = await _stream(answer, request)
    assert len(sent) == 2 and isinstance(events[-1], Done)
    history = json.loads(sent[1].content)["messages"]
    assert history[1] == {"role": "assistant", "content": _blocks("SC")}
    assert history[3] == {"role": "assistant", "content": [result]}
    calls = [b for b in events[-1].message.content if isinstance(b, HostedToolBlock)]
    assert len(calls) == 1 and calls[0].native.payload == (result,)
    assert events[-1].message.hosted_summary.counts == {"web_search_requests": 1}


async def test_a_steer_waits_until_the_deferred_result_and_never_enters_the_tool_result_user_message():
    from core.agent_core.agent.loop import Agent
    from core.agent_core.tools.base import ToolResult
    from tests.agent_core.ai.test_hosted_replay import _Hops, _hosted_tool
    from tests.agent_core.fakes import FakeTool, FakeJobHost, InMemoryTranscriptStore, input_row

    sent = []
    def gateway(wire):
        sent.append(json.loads(wire.content))
        if len(sent) == 1:
            blocks, stop = _blocks("SC"), "tool_use"
        elif len(sent) == 2:
            blocks, stop = [{**RESULT, "tool_use_id": "srvtoolu_0"}, {"type": "text", "text": "found"}], "end_turn"
        else:
            blocks, stop = [{"type": "text", "text": "steer answered"}], "end_turn"
        return httpx.Response(200, headers=_served(), text=_sse(blocks, stop=stop, searches=1 if len(sent) == 2 else 0))

    async def read(arguments, ctx):
        assert await agent.steer(input_row("steer", "extra user input"))
        return ToolResult((TextBlock("contents"),))

    store = InMemoryTranscriptStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        agent = Agent(session_id="session", models=_Hops(client, (_request().endpoint,)),
                      tools=(_hosted_tool("web_search"), FakeTool("read", execute=read)), hooks=(),
                      store=store, jobs=FakeJobHost(), cwd="/test-owned")
        events = [e async for e in agent.run(input_row("input", "search"), turn_id="turn")]
    assert events[-1].reason == "completed" and len(sent) == 3
    assert {b["type"] for b in sent[1]["messages"][-1]["content"]} == {"tool_result"}
    assert sent[1]["tools"] == sent[0]["tools"]
    assert "extra user input" not in json.dumps(sent[1])
    assert "extra user input" in json.dumps(sent[2])


def _completion(paused):
    blocks = []
    for call in paused.content:
        if not isinstance(call, HostedToolBlock) or call.record.status != "pending":
            continue
        result = {**RESULT, "tool_use_id": call.id}
        completed = anthropic_hosted_block(call.native.payload[0], result, source_id=SOURCE)
        blocks.append(replace(completed, native=replace(completed.native, payload=(result,))))
    return AssistantMessage((*blocks, TextBlock("found")), paused.origin, "stop", hosted_summary=HostedSummary())


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("shape", SHAPES)
async def test_later_replay_pairs_once_but_preserves_native_response_boundaries(native, shape):
    request, paused = await _paused(shape)
    completed = _completion(paused)
    endpoint = replace(request.endpoint, hosted_tools={
        "web_search": replace(request.endpoint.hosted_tools["web_search"], replay=native),
    })
    request = replace(request, endpoint=endpoint, messages=(*request.messages, completed, UserMessage((TextBlock("next"),))))
    wire = await build_wire_request(request, gateway=True)
    history = wire.body["messages"]
    assert "interrupted" not in json.dumps(history)
    if native:
        assert history[1] == {"role": "assistant", "content": _blocks(shape)}
        assert history[3]["content"][0] == completed.content[0].native.payload[0]
        assert sum(b.get("type") == "server_tool_use" for m in history if isinstance(m["content"], list) for b in m["content"]) == len(shape) - shape.count("C")
    else:
        assert {b["type"] for b in history[1]["content"]} == {"tool_use"}
        assert [b["name"] for b in history[1]["content"]] == ["read" if kind == "C" else "web_search" for kind in shape]
        results = history[2]["content"]
        assert len(results) == len(shape) and all(b["type"] == "tool_result" for b in results)
        assert "https://avibe.bot/r" in json.dumps(results)
        assert history[3] == {"role": "assistant", "content": [{"type": "text", "text": "found"}]}


async def test_each_responses_cost_stays_on_a_card_that_ran_and_is_never_counted_again():
    from core.agent_core.messages import HostedCost
    from core.hosted_activity import native_activities
    request, paused = await _paused("FSC")
    paused = replace(paused, hosted_summary=HostedSummary(cost=HostedCost("list", usd=0.01)))
    completed = replace(_completion(paused), hosted_summary=HostedSummary(cost=HostedCost("list", usd=0.01)))
    first, second = native_activities(paused), native_activities(completed)
    assert len(first) == len(second) == 1
    assert first[0].cost.usd == 0.01
    assert second[0].cost.usd == 0.01
    assert sum(card.cost.usd for card in (*first, *second) if card.cost) == 0.02
    assert first[0].cost_calls == second[0].cost_calls == 1


@pytest.mark.parametrize("cut", ["prior-summarized", "completion-aborted"])
async def test_a_missing_or_unreplayable_half_never_leaves_an_orphan_native_fragment(cut):
    request, paused = await _paused()
    completed = _completion(paused)
    endpoint = replace(request.endpoint, hosted_tools={
        "web_search": replace(request.endpoint.hosted_tools["web_search"], replay=True),
    })
    if cut == "prior-summarized":
        history = (_request().messages[0], completed)
    else:
        history = (*request.messages, replace(completed, stop_reason="aborted"))
    wire = await build_wire_request(replace(request, endpoint=endpoint, messages=history), gateway=True)
    body = json.dumps(wire.body["messages"])
    assert "server_tool_use" not in body and "web_search_tool_result" not in body
    assert "https://avibe.bot/r" in body and "interrupted" not in body
