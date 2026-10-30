"""Search-history repair, not a blanket invalid-request retry. Scenario MH-HOSTED-VIBEY-005.

Existing opaque tests omit deferred native calls and reject only before output. These
regressions exercise the real wire compiler and adapter with that missing shape.
"""

from dataclasses import replace
import json

import httpx
import pytest

from core.agent_core.agent.loop import Agent
from core.agent_core.ai.provider import ProviderError, Refusal
from core.agent_core.messages import (
    AssistantMessage, HostedAction, HostedNative, HostedRecord, HostedSummary,
    HostedToolBlock, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultMessage, UserMessage,
)
from tests.agent_core.ai import test_hosted_replay as replay
from tests.agent_core.fakes import FakeJobHost, FakeTool, InMemoryTranscriptStore, input_row

PREFILL = "This model does not support assistant message prefill. The conversation must end with a user message."
SEARCH_ERROR = "Invalid encrypted_content in search_result block"
LIVE_USE = {
    "type": "server_tool_use", "id": "srvtoolu_live", "name": "web_search", "input": {"query": "next release"},
}
LIVE_RESULT = {
    "type": "web_search_tool_result", "tool_use_id": "srvtoolu_live",
    "content": [{"type": "web_search_result", "title": "Next", "url": "https://avibe.bot/next", "encrypted_content": "new-opaque"}],
}


def mixed_request(*, history=True):
    endpoint = replay._endpoint("anthropic", replay=True)
    old = replay._searched("anthropic")
    pending = HostedToolBlock(
        "srvtoolu_live", "web_search", HostedAction("search", queries=("next release",)),
        HostedRecord(status="pending"), HostedNative(source_id=replay.SOURCE, payload=(LIVE_USE,)),
    )
    live = AssistantMessage(
        (ThinkingBlock(text="read first", signature="live-signature"), pending,
         ToolCallBlock("toolu_read", "read", {"path": "a"})),
        old.origin, "tool_use", hosted_summary=HostedSummary(), source_identity=replay.IDENTITY,
    )
    earlier = (old, UserMessage((TextBlock("next"),))) if history else ()
    request = replay._request(
        endpoint, *earlier, live, ToolResultMessage("toolu_read", "read", (TextBlock("saved result"),)),
    )
    return replace(request, tools=(replay.WEB, FakeTool("read").spec))


def failed_stream(detail, output="text"):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 7}}}]
    if output == "text":
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial answer"}},
            {"type": "content_block_stop", "index": 0},
        ]
    elif output == "thinking":
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "partial thought"}},
        ]
    elif output == "search":
        events += [
            {"type": "content_block_start", "index": 0, "content_block": LIVE_RESULT},
            {"type": "content_block_stop", "index": 0},
        ]
    elif output in {"tool", "complete-tool"}:
        events += [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_unfinished", "name": "read", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path": "a"}' if output == "complete-tool" else '{"path":'}},
        ]
        if output == "complete-tool":
            events.append({"type": "content_block_stop", "index": 0})
    events.append({"type": "error", "error": {"type": "invalid_request_error", "message": detail}})
    return replay._sse("anthropic", tuple(events))


@pytest.mark.parametrize("detail", [SEARCH_ERROR, "Invalid encrypted_index in citation", PREFILL])
@pytest.mark.parametrize("transport", ["http", "wrapped", "stream-text", "stream-thinking", "stream-tool"])
async def test_search_recovery_is_identified_on_the_actual_wire_even_after_output(detail, transport):
    request = mixed_request()
    if transport.startswith("stream-"):
        response = httpx.Response(200, headers=replay._served("anthropic"), text=failed_stream(detail, transport[7:]))
    else:
        response = httpx.Response(400, json=replay._anthropic_refusal(detail, via_gateway=transport == "wrapped"))

    sent, events = await replay._send(request, lambda _: response)

    assert len(sent) == 1  # Classification only; the Agent owns recovery.
    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.refusal == Refusal(
        "search_history", replay.SOURCE, replay.IDENTITY, "web_search", "replay",
    )
    assert not error.retryable and not error.continuable
    assert (error.partial is not None) == transport.startswith("stream-")


@pytest.mark.parametrize("transport", ["http", "thinking"])
@pytest.mark.parametrize("case", ["assistant-tail", "no-history", "wrong-result", "extra-user-text", "unrelated", "signature", "portable"])
async def test_prefill_is_not_an_unconditional_search_refusal(case, transport):
    request = mixed_request(history=case != "no-history")
    detail = PREFILL
    if case == "assistant-tail":
        request = replace(request, messages=request.messages[:-1])
    elif case == "wrong-result":
        request = replace(request, messages=(*request.messages[:-1], replace(request.messages[-1], tool_call_id="unrelated")))
    elif case == "extra-user-text":
        request = replace(request, messages=(*request.messages, UserMessage((TextBlock("continue"),))))
    elif case == "unrelated":
        detail = "invalid max_tokens"
    elif case == "signature":
        detail = "Invalid signature in thinking block"
    elif case == "portable":
        route = replace(request.endpoint.hosted_tools["web_search"], replay=False)
        request = replace(request, endpoint=replace(request.endpoint, hosted_tools={"web_search": route}))

    response = (
        httpx.Response(200, headers=replay._served("anthropic"), text=failed_stream(detail, "thinking"))
        if transport == "thinking"
        else httpx.Response(400, json=replay._anthropic_refusal(detail, via_gateway=False))
    )
    _, events = await replay._send(request, lambda _: response)

    error = events[-1]
    assert isinstance(error, ProviderError)
    assert error.kind == "invalid_request" and not error.retryable and not error.continuable
    assert error.refusal is None or error.refusal.kind != "search_history"


def mixed_stream(*, completed=False):
    events = (
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "read first", "signature": "live-signature"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": LIVE_USE},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_read", "name": "read", "input": {"path": "a"}}},
        {"type": "content_block_stop", "index": 2},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    )
    if completed:
        # The current search already finished before the client tool; its ciphertext can independently be refused.
        tail = tuple({**event, "index": 3} if "index" in event else event for event in events[5:])
        events = (*events[:5],
                  {"type": "content_block_start", "index": 2, "content_block": LIVE_RESULT},
                  {"type": "content_block_stop", "index": 2}, *tail)
    return replay._sse("anthropic", events)


def completed_stream():
    events = (
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": LIVE_RESULT},
        {"type": "content_block_stop", "index": 0},
        *replay._ANSWERED["anthropic"][1:],
    )
    # The final text has its own index after the result.
    events = tuple({**event, "index": 1} if event.get("index") == 0 and i > 2 else event for i, event in enumerate(events))
    return replay._sse("anthropic", events)


@pytest.mark.parametrize("detail", [PREFILL, SEARCH_ERROR])
@pytest.mark.parametrize("again", [False, True])
async def test_only_completed_history_is_demoted_and_the_client_tool_is_not_rerun(monkeypatch, detail, again):
    read = FakeTool("read")
    monkeypatch.setattr(replay, "READ", read)

    def answer(turn, attempt):
        if turn == 1:
            return httpx.Response(200, headers=replay._served("anthropic"), text=replay._searched_and_answered())
        if turn == 2:
            if attempt == 1:
                return httpx.Response(200, headers=replay._served("anthropic"), text=mixed_stream())
            if attempt == 2 or again:
                return httpx.Response(400, json=replay._anthropic_refusal(detail, via_gateway=True))
            return httpx.Response(200, headers=replay._served("anthropic"), text=completed_stream())
        return httpx.Response(200, headers=replay._served("anthropic"), text=replay._sse("anthropic", replay._ANSWERED["anthropic"]))

    sent, ended = await replay._session(answer, replay._endpoint("anthropic", replay=True), turns=2 if again else 3)

    assert len(read.calls) == 1
    assert len(sent[1]) == 3  # The failed continuation has exactly one repair attempt.
    assert ended[1].reason == ("error" if again else "completed")
    before, after = [json.loads(wire.content) for wire in sent[1][1:]]
    before_blocks, after_blocks = (replay._history_items("anthropic", body) for body in (before, after))
    assert replay.RESULT in before_blocks and replay.RESULT not in after_blocks
    assert LIVE_USE in after_blocks
    assert before["messages"][-2:] == after["messages"][-2:]  # Preserve signed thinking and the saved result.
    assert before["tools"] == after["tools"]
    assert "avibe release" in json.dumps(after) and "https://avibe.bot/r" in json.dumps(after)
    if not again:
        assert len(sent[2]) == 1 and ended[2].reason == "completed"
        # A guarded trial does not teach permanent capability evidence or extend the persisted payload vocabulary.
        assert '"opaque"' in sent[2][0].content.decode()


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("again", [False, True])
async def test_an_explicit_in_flight_ciphertext_refusal_still_recovers_after_the_history_trial(monkeypatch, partial, again):
    """Demoting history must not wedge a Session when the next rejection names current ciphertext."""
    read = FakeTool("read")
    monkeypatch.setattr(replay, "READ", read)

    def answer(turn, attempt):
        if turn == 1:
            return httpx.Response(200, headers=replay._served("anthropic"), text=replay._searched_and_answered())
        if attempt == 1:
            return httpx.Response(200, headers=replay._served("anthropic"), text=mixed_stream(completed=True))
        if partial and attempt == 3:
            return httpx.Response(200, headers=replay._served("anthropic"), text=failed_stream(SEARCH_ERROR))
        if attempt in (2, 3) or again:
            return httpx.Response(400, json=replay._anthropic_refusal(SEARCH_ERROR, via_gateway=True))
        return httpx.Response(200, headers=replay._served("anthropic"), text=replay._sse("anthropic", replay._ANSWERED["anthropic"]))

    sent, ended = await replay._session(answer, replay._endpoint("anthropic", replay=True), turns=2)

    assert ended[1].reason == ("error" if again else "completed") and len(sent[1]) == 4 and len(read.calls) == 1
    history_repaired = json.loads(sent[1][2].content)
    fully_repaired = json.loads(sent[1][3].content)
    assert '"opaque"' not in json.dumps(history_repaired) and '"new-opaque"' in json.dumps(history_repaired)
    assert "encrypted_content" not in json.dumps(fully_repaired)
    assert "live-signature" in json.dumps(fully_repaired)
    assert "https://avibe.bot/next" in json.dumps(fully_repaired)
    assert history_repaired["messages"][-1] == fully_repaired["messages"][-1]


@pytest.mark.parametrize("path", ["messages.1.content.1", "messages.3.content.1", "messages.9.content.0", "messages.90.content.1"])
@pytest.mark.parametrize("history", [False, True])
async def test_a_search_error_path_is_a_hint_but_actual_request_ciphertext_is_required(path, history):
    request = mixed_request(history=history)
    _, events = await replay._send(request, lambda _: httpx.Response(
        400, json=replay._anthropic_refusal(f"{path}: {SEARCH_ERROR}", via_gateway=False),
    ))
    error = events[-1]
    assert isinstance(error, ProviderError)
    assert (error.refusal.kind if error.refusal else None) == ("search_history" if history else None)


async def test_a_resolvable_path_selects_the_current_ciphertext_instead_of_unrelated_history():
    request = mixed_request()
    live = request.messages[-2]
    current = live.content[1]
    finished = replace(current, record=HostedRecord(status="completed"), native=replace(current.native, payload=(LIVE_USE, LIVE_RESULT)))
    request = replace(request, messages=(*request.messages[:-2], replace(live, content=(live.content[0], finished, live.content[2])), request.messages[-1]))
    _, events = await replay._send(request, lambda _: httpx.Response(
        400, json=replay._anthropic_refusal(f"messages.3.content.2: {SEARCH_ERROR}", via_gateway=False),
    ))
    assert isinstance(events[-1], ProviderError)
    assert events[-1].refusal == Refusal("opaque_refused", replay.SOURCE, replay.IDENTITY, "web_search", "replay", ("hosted",))


@pytest.mark.parametrize("action", ["steer", "stop"])
async def test_control_input_during_a_search_refusal_does_not_break_the_saved_tool_continuation(action):
    """A recompile after failed deltas must not admit a steer early or bypass Stop.

    Existing deferred-steer tests have no rejected request or changed-request loop between the result and completion.
    """
    seen, read, store = [], FakeTool("read"), InMemoryTranscriptStore()

    async def gateway(wire):
        seen.append(json.loads(wire.content))
        if len(seen) == 1:
            body = replay._searched_and_answered()
        elif len(seen) == 2:
            body = mixed_stream()
        elif len(seen) == 3:
            if action == "steer":
                assert await agent.steer(input_row("steer", "extra user input"))
            else:
                agent.abort("user stop")
            body = failed_stream(PREFILL)
        elif len(seen) == 4:
            body = completed_stream()
        else:
            body = replay._sse("anthropic", replay._ANSWERED["anthropic"])
        return httpx.Response(200, headers=replay._served("anthropic"), text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
        for turn in (1, 2):
            agent = Agent(
                session_id="session", models=replay._Hops(client, (replay._endpoint("anthropic", replay=True),)),
                tools=(replay._hosted_tool("web_search"), read), hooks=(), store=store, jobs=FakeJobHost(), cwd="/test-owned",
            )
            events = [event async for event in agent.run(input_row(f"input-{turn}", "search"), turn_id=f"turn-{turn}")]

    assert len(read.calls) == 1
    assert events[-1].reason == ("completed" if action == "steer" else "aborted")
    assert len(seen) == (5 if action == "steer" else 3)
    rows = await store.load("session")
    assert sum(row.kind == "tool_result" and row.message.tool_call_id == "toolu_read" for row in rows) == 1
    if action == "steer":
        assert seen[2]["messages"][-2:] == seen[3]["messages"][-2:]
        assert "extra user input" not in json.dumps(seen[3])
        assert "extra user input" in json.dumps(seen[4])
        assert sum(row.kind == "input" and row.row_id == "steer" for row in rows) == 1
