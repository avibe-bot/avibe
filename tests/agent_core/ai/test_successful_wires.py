"""Receipts describe the actual completed adapter POST, not its preview or preceding attempts."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter
from core.agent_core.ai.provider import Done, ProviderError
from core.agent_core.cancel import CancelToken
from tests.agent_core.agent.test_compaction import (
    CHECKPOINT, ROOMY, SELECTION, Model, cross, history, make_agent, reader, run, tokens,
)
from tests.agent_core.ai.test_hosted_pause_turn import _request, _response, _served, _text
from tests.agent_core.ai.test_request_artifacts import _direct_request, _run
from tests.agent_core.fakes import InMemoryTranscriptStore


CHAT = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
RESPONSES = 'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
GOOGLE = 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]},"finishReason":"STOP"}]}\n\n'


def messages(stop="end_turn"):
    return _response(_text("yes"), stop_reason=stop, input_tokens=10, output_tokens=1)


@pytest.mark.parametrize("protocol,kind,body", [
    ("anthropic", "subscription", messages()), ("openai_chat", "relay_key", CHAT),
    ("openai_responses", "official_api_key", RESPONSES), ("google", "unknown", GOOGLE),
], ids=["messages", "chat", "responses", "gemini"])
async def test_successful_wire_counts_exact_http_bytes_and_actual_serving_source(protocol, kind, body):
    receipts = []
    req = replace(_direct_request(protocol), system='snow 雪\n"', successful_wires=receipts)
    source = "src_served0001"
    hop = {"source": source, "model": req.endpoint.model_id, "api": protocol, "provider": req.endpoint.provider}
    headers = {"x-avibe-served-hop": json.dumps(hop), "x-avibe-source-kind": kind}
    sent, events = await _run(req, lambda _: httpx.Response(200, text=body, headers=headers), None, gateway=True)
    assert len(sent) == 1 and isinstance(events[-1], Done)
    assert receipts == [{"body_bytes": len(sent[0].content), "source_id": source, "source_kind": kind}]
    receipts.clear()
    _, errors = await _run(req, lambda _: httpx.Response(200, text="data: {"), None, gateway=True)
    assert isinstance(errors[-1], ProviderError) and not receipts


@pytest.mark.parametrize("reported,kind,expected", [(True, "not-a-kind", "src_served0001"), (False, "subscription", None)])
async def test_untrusted_attribution_never_defaults_to_the_requested_route(reported, kind, expected):
    req = replace(_direct_request("openai_chat"), successful_wires=[])
    headers = {**_served(source="src_served0001", reported=reported), "x-avibe-source-kind": kind}
    sent, _ = await _run(req, lambda _: httpx.Response(200, text=CHAT, headers=headers), None, gateway=True)
    assert req.successful_wires == [{"body_bytes": len(sent[0].content), "source_id": expected, "source_kind": "unknown"}]


async def test_stop_before_send_leaves_no_receipt():
    req = replace(_direct_request("openai_chat"), successful_wires=[])
    cancel = CancelToken()
    cancel.cancel()
    sent, _ = await _run(req, lambda _: pytest.fail("never sent"), None, cancel=cancel)
    assert sent == [] and req.successful_wires == []


@pytest.mark.parametrize("second", [200, 503])
async def test_pause_turn_counts_the_first_completed_wire_even_if_the_second_fails(second):
    req = replace(_request(), successful_wires=[])
    headers = {**_served(), "x-avibe-source-kind": "subscription"}
    answers = iter([httpx.Response(200, headers=headers, text=messages("pause_turn")),
                    httpx.Response(second, headers=headers, text=messages())])
    sent, events = await _run(req, lambda _: next(answers), None, gateway=True)
    assert len(sent) == 2 and isinstance(events[-1], Done if second == 200 else ProviderError)
    assert [row["body_bytes"] for row in req.successful_wires] == [len(w.content) for w in sent[:2 if second == 200 else 1]]


async def test_stop_during_pause_followup_persists_only_completed_wire():
    sent = []
    following = asyncio.Event()

    async def reply(wire):
        sent.append(wire)
        if len(sent) == 1:
            return httpx.Response(200, headers=_served(), text=messages("pause_turn"))
        following.set()
        await asyncio.Event().wait()

    store = InMemoryTranscriptStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        req = _request()
        agent = make_agent(AnthropicAdapter(client), store=store, selection=replace(SELECTION, endpoint=req.endpoint))
        task = asyncio.create_task(run(agent))
        try:
            await asyncio.wait_for(following.wait(), 3)
        finally:
            agent.abort()
            await asyncio.wait_for(task, 3)
    assert len(sent) == 2
    assert [w["body_bytes"] for _, _, a in store.attempts for w in a["cache"]["successful_wires"]] == [len(sent[0].content)]


async def test_fork_tool_rounds_and_conversation_each_own_their_completed_wire():
    store = InMemoryTranscriptStore()
    tools = [reader(tokens(3_000))]
    await run(make_agent(Model(history()), store=store, tools=tools, selection=ROOMY))
    call = [
        {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "read", "id": "read_1", "input": {}}},
        {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"path":"f"}'}},
    ]
    answers = iter([
        _response(call, stop_reason="tool_use", input_tokens=10, output_tokens=1),
        _response(_text(CHECKPOINT), stop_reason="end_turn", input_tokens=10, output_tokens=1),
        messages(),
    ])
    sent = []

    def reply(wire):
        sent.append(len(wire.content))
        return httpx.Response(200, text=next(answers))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await cross(make_agent(AnthropicAdapter(client), store=store, tools=tools, selection=ROOMY))
    assert result[-1].reason == "completed" and len(sent) == 3
    (audit,) = [a for _, _, a in store.audits]
    side = [w["body_bytes"] for a in audit["cache_attempts"] for w in a["cache"]["successful_wires"]]
    conversation = [w["body_bytes"] for row in store.rows["session"] if row.kind == "response"
                    for w in row.payload["cache"]["successful_wires"]]
    assert side == sent[:2] and conversation == sent[2:]
    assert not store.attempts
