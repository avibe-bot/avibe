"""C-9 through the loop, observed at the recording provider and the store.

These cover what the pure rules in ``test_context.py`` cannot: when the loop
applies them, what the forked checkpoint request carries, what the checkpoint
turn may execute, how the overflow ladder steps, and the guards. The window is
the fakes' 32,000 tokens with 4,096 output, so T = 19,904 and keep = 4,976.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from copy import deepcopy
from dataclasses import replace

import pytest

from core.agent_core.agent.checkpoint import BUDGET_USED, DENIED
from core.agent_core.agent.events import (
    AgentError,
    AssistantTextDelta,
    CompactionFailed,
    CompactionFinished,
    CompactionPaused,
    CompactionStarted,
    ContextExhausted,
    MessageCommitted,
    RunEnded,
    ToolStarted,
)
from core.agent_core.agent.loop import Agent
from core.agent_core.ai._common import endpoint_origin, prepare_messages
from core.agent_core.ai.anthropic import build_messages_payload
from core.agent_core.ai.openai_chat import build_chat_payload
from core.agent_core.ai.openai_responses import build_responses_payload
from core.agent_core.ai.provider import Done, ModelCapabilities, ModelEndpoint, ProviderError, TextDelta
from core.agent_core.agent.models import ModelSelection
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.context import (
    CHECKPOINT_REQUEST,
    ContextConfig,
    StateRequest,
    request_tokens,
)
from core.agent_core.harness.projection import project
from core.agent_core.messages import (
    AssistantMessage,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
    text,
)
from core.agent_core.tools.base import ToolResult
from tests.agent_core.fakes import (
    ENDPOINT,
    SELECTION,
    FakeJobHost,
    FakeModelRouter,
    FakeTool,
    InMemoryTranscriptStore,
    assistant,
    input_row,
)

THRESHOLD = 19_904
CHECKPOINT = "# 1. Self and method\n- the checkpoint"


def tokens(count: int) -> str:
    return "x" * (4 * count)


def is_checkpoint(request) -> bool:
    return any(
        isinstance(message, UserMessage)
        and message.content[0].text is not None
        and message.content[0].text.startswith("<context-checkpoint-request>")
        for message in request.messages
    )


def call(name: str, call_id: str, **arguments) -> list[Done]:
    return [Done(assistant(calls=[ToolCallBlock(call_id, name, arguments)]))]


class Model:
    """Answers conversation requests and checkpoint-turn requests from separate scripts."""

    protocol = "anthropic"

    def __init__(self, conversation=(), checkpoint=()) -> None:
        self.conversation = deque(conversation)
        self.checkpoint = deque(checkpoint)
        self.requests: list = []

    @property
    def conversation_requests(self):
        return [request for request in self.requests if not is_checkpoint(request)]

    @property
    def checkpoint_requests(self):
        return [request for request in self.requests if is_checkpoint(request)]

    async def stream(self, request, cancel):
        self.requests.append(deepcopy(request))
        queue = self.checkpoint if is_checkpoint(request) else self.conversation
        if not queue:
            raise AssertionError("unscripted request")
        script = queue.popleft()
        for event in script(request) if callable(script) else script:
            yield deepcopy(event)


class Host:
    def __init__(self) -> None:
        self.states: list[StateRequest] = []
        self.records: list[tuple[str, int]] = []

    def earlier_record(self, session_id, through_seq):
        self.records.append((session_id, through_seq))
        return f"lookup {session_id} through {through_seq}"

    async def render_state(self, request):
        self.states.append(request)
        return [f"<state skills={len(request.skills)} mid_turn={request.mid_turn}/>"]


def make_agent(model, *, store=None, tools=(), hooks=(), selection=SELECTION, context=None, cwd="/test-owned"):
    return Agent(
        session_id="session",
        models=FakeModelRouter(model, (selection,)),
        tools=tools,
        hooks=hooks,
        store=store or InMemoryTranscriptStore(),
        jobs=FakeJobHost(),
        cwd=cwd,
        context=context or ContextConfig(),
    )


async def run(agent, value="go", row="input"):
    return [event async for event in agent.run(input_row(row, value), turn_id=row)]


def reader(output: str) -> FakeTool:
    return FakeTool("read", result=ToolResult((text(output),)))


def sized_reader(*sizes: int) -> FakeTool:
    """A ``read`` whose results have the given sizes in tokens, in call order."""
    remaining = deque(sizes)

    async def execute(arguments, ctx):
        return ToolResult((text(tokens(remaining.popleft())),))

    return FakeTool("read", execute=execute)


def reads(count: int, final: str = "done"):
    return [call("read", f"read-{index}", path="f") for index in range(count)] + [[Done(assistant(final))]]


def history():
    """A first run whose context is past ``keep``, so a manual checkpoint has something to summarize."""
    return [call("read", "h0", path="old"), call("read", "h1", path="old"), [Done(assistant("first"))]]


def growing(count: int, final: str = "done"):
    """``count`` reads of 6,000 tokens each, then a final reply; the fifth request crosses T."""
    return [call("read", f"read-{index}", path=f"f{index}") for index in range(count)] + [[Done(assistant(final))]]


async def test_a_threshold_checkpoint_forks_the_exact_prefix_and_leaves_checkpoint_and_tail():
    host = Host()
    model = Model(growing(4), [[TextDelta(0, "streamed checkpoint"), Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))], context=ContextConfig(host=host))
    events = await run(agent)

    previous, fork, after = model.requests[3], model.requests[4], model.requests[5]
    assert [is_checkpoint(request) for request in model.requests] == [False] * 4 + [True, False]
    # Cache safety: the fork is the previous request extended, with every cache-relevant setting unchanged.
    assert fork.messages[: len(previous.messages)] == previous.messages
    assert fork.messages[-1].content[0].text.startswith(CHECKPOINT_REQUEST)
    for field in ("endpoint", "system", "tools", "reasoning_effort", "supports_images", "cache", "max_tokens"):
        assert getattr(fork, field) == getattr(previous, field)

    rows = await agent.store.load("session")
    compaction = next(row for row in rows if row.kind == "compaction")
    before = [row for row in rows if row.context_seq < compaction.context_seq]
    assert fork.messages[:-1] == project(before).messages  # the whole conversation, then the request
    assert compaction.payload["mode"] == "normal" and compaction.payload["reason"] == "threshold"
    assert compaction.payload["checkpoint"] == CHECKPOINT
    assert host.states == [StateRequest("session", (), True)]
    assert host.records == [("session", compaction.payload["summarized_to_seq"])]

    # The next request: the checkpoint message (summary, then state), then the kept tool batch.
    checkpoint, *tail = after.messages
    summary, state = (block.text for block in checkpoint.content)
    assert CHECKPOINT in summary and "<current-request>\ngo\n</current-request>" in summary
    assert "lookup session through" in summary and state == "<state skills=0 mid_turn=True/>"
    assert [type(message).__name__ for message in tail] == ["AssistantMessage", "ToolResultMessage"]
    assert tail[0].tool_calls[0].id == "read-3"
    assert after.messages == project(rows[: len(rows) - 1]).messages
    assert request_tokens(after.system, after.tools, after.messages) < THRESHOLD

    # Silent: nothing streamed or executed by the checkpoint turn reaches the events.
    assert [event.reason for event in events if isinstance(event, CompactionFinished)] == ["threshold"]
    assert not [event for event in events if isinstance(event, AssistantTextDelta)]
    assert len([event for event in events if isinstance(event, MessageCommitted)]) == 5
    assert events[-1] == RunEnded("input", events[-1].seq, "completed")
    (session_id, _, audit), = agent.store.audits
    assert (audit["outcome"], audit["compaction_event_id"]) == ("completed", compaction.row_id)
    assert [message["role"] for message in audit["messages"]] == ["assistant"]
    # Append-only: every row before the checkpoint is exactly what was committed before it.
    assert all(row.kind != "compaction" for row in before) and rows.index(compaction) == len(before)


def _strip_cache_markers(value):
    if isinstance(value, dict):
        return {key: _strip_cache_markers(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_strip_cache_markers(item) for item in value]
    return value


async def _wire(request):
    prepared = await prepare_messages(
        request.messages,
        target=endpoint_origin(request.endpoint),
        supports_images=request.supports_images,
        protocol=request.endpoint.protocol,
        media_loader=None,
        cancel=CancelToken(),
    )
    messages, images = prepared
    build = {
        "anthropic": build_messages_payload,
        "openai_chat": build_chat_payload,
        "openai_responses": build_responses_payload,
    }[request.endpoint.protocol]
    payload = _strip_cache_markers(build(request, messages, loaded_images=images))
    key = "input" if request.endpoint.protocol == "openai_responses" else "messages"
    items = payload.pop(key)
    return payload, [json.dumps(item, ensure_ascii=False, sort_keys=True).encode() for item in items]


@pytest.mark.parametrize("protocol", ["anthropic", "openai_chat", "openai_responses"])
async def test_the_fork_is_a_byte_identical_prefix_on_every_protocol_wire(protocol):
    endpoint = ModelEndpoint(protocol, "http://model.invalid", "test-model", "", provider="test-provider")
    capabilities = replace(SELECTION.capabilities, supports_reasoning=True, reasoning_efforts=("high",))
    selection = ModelSelection(endpoint, capabilities)
    model = Model(growing(4), [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))], selection=selection)
    agent.reasoning_effort = "high"
    await run(agent)
    previous, fork = model.requests[3], model.requests[4]
    assert is_checkpoint(fork) and fork.reasoning_effort == "high"
    previous_settings, previous_items = await _wire(previous)
    fork_settings, fork_items = await _wire(fork)
    assert fork_settings == previous_settings  # model, system, tools, tool choice, thinking, effort, output cap
    assert fork_items[: len(previous_items)] == previous_items
    assert len(fork_items) > len(previous_items)


def scratch_case(tmp_path):
    scratch = tmp_path / "scratch" / "session"
    outside = tmp_path / "outside"
    scratch.mkdir(parents=True)
    outside.mkdir()
    (scratch / "escape").symlink_to(outside, target_is_directory=True)
    return scratch, outside


async def test_the_checkpoint_policy_allows_reads_and_scratch_writes_and_never_runs_anything_else(tmp_path):
    scratch, outside = scratch_case(tmp_path)
    cwd = str(tmp_path)
    tools = {name: FakeTool(name) for name in ("write", "edit", "bash", "other")}
    tools["read"] = reader(tokens(3_000))
    calls = [
        ToolCallBlock("bash", "bash", {"command": "rm -rf /"}),
        ToolCallBlock("read", "read", {"path": "notes.md"}),
        ToolCallBlock("write-in", "write", {"path": "scratch/session/plan.md", "content": "ok"}),
        ToolCallBlock("edit-in", "edit", {"path": str(scratch / "plan.md"), "oldText": "a", "newText": "b"}),
        ToolCallBlock("write-out", "write", {"path": str(outside / "f"), "content": "no"}),
        ToolCallBlock("write-up", "write", {"path": "scratch/session/../../outside/f", "content": "no"}),
        ToolCallBlock("write-link", "write", {"path": "scratch/session/escape/f", "content": "no"}),
        ToolCallBlock("edit-out", "edit", {"path": "notes.md", "edits": [{"oldText": "a", "newText": "b"}]}),
        ToolCallBlock("other", "other", {}),
    ]
    model = Model(
        history(),
        [[Done(AssistantMessage(tuple(calls), assistant().origin, "tool_use"))], [Done(assistant(CHECKPOINT))]],
    )
    context = ContextConfig(scratch_dir=str(scratch))
    agent = make_agent(model, tools=list(tools.values()), context=context, cwd=cwd)
    await run(agent)
    before = await agent.store.load("session")
    events = [event async for event in agent.compact(turn_id="compact")]

    executed = {name: [ctx.tool_call_id for _, ctx in tool.calls] for name, tool in tools.items()}
    assert executed == {
        "read": ["h0", "h1", "read"],
        "write": ["write-in"],
        "edit": ["edit-in"],
        "bash": [],
        "other": [],
    }
    results = {message.tool_call_id: message for message in model.requests[-1].messages[-len(calls) :]}
    denied = {key for key, message in results.items() if message.content[0].text == DENIED}
    assert denied == {"bash", "write-out", "write-up", "write-link", "edit-out", "other"}
    assert all(results[key].is_error for key in denied)
    # The turn's own calls and results are audit only: the context has none of them.
    rows = await agent.store.load("session")
    assert rows[: len(before)] == before
    assert [row.kind for row in rows[len(before) :]] == ["compaction"]
    assert not [event for event in events if isinstance(event, ToolStarted)]
    assert len(agent.store.audits[0][2]["messages"]) == 2 + len(calls)
    assert events[-1].reason == "completed"


async def test_the_checkpoint_turn_gets_five_tool_rounds_then_must_write():
    tools = [reader(tokens(3_000))]
    rounds = [call("read", f"r{index}", path="f") for index in range(7)]
    model = Model(history(), rounds)
    agent = make_agent(model, tools=tools)
    await run(agent)
    before = await agent.store.load("session")
    tools[0].result = ToolResult((text("small"),))
    events = [event async for event in agent.compact(turn_id="compact")]
    assert [ctx.tool_call_id for _, ctx in tools[0].calls] == ["h0", "h1", "r0", "r1", "r2", "r3", "r4"]
    sixth = model.requests[-1].messages[-1]
    assert (sixth.tool_call_id, sixth.content[0].text) == ("r5", BUDGET_USED)
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert len(failed) == 1 and "kept calling tools" in failed[0].error
    after = await agent.store.load("session")
    assert after[: len(before)] == before and [row.kind for row in after[len(before) :]] == ["agent_state"]
    assert after[-1].payload["context"] == {"failures": 1, "ineffective": 0, "paused": False}
    assert events[-1].reason == "error"


async def test_at_the_threshold_the_checkpoint_turn_may_not_grow_the_context():
    tools = [reader(tokens(6_000)), FakeTool("look")]
    checkpoint_turn = [call("look", "look", path="f"), [Done(assistant(CHECKPOINT))]]
    model = Model(growing(4), checkpoint_turn)
    agent = make_agent(model, tools=tools)
    await run(agent)
    assert not tools[1].calls
    denied = model.checkpoint_requests[-1].messages[-1]
    assert denied.content[0].text == DENIED  # not in the table at all
    model = Model(growing(4), [call("read", "peek", path="f"), [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))])
    await run(agent)
    assert [ctx.tool_call_id for _, ctx in agent._tools["read"].calls] == [f"read-{index}" for index in range(4)]
    assert model.checkpoint_requests[-1].messages[-1].content[0].text == BUDGET_USED


FAILURES = [
    [Done(assistant(CHECKPOINT, stop_reason="length"))],
    [Done(AssistantMessage((ThinkingBlock("only thinking"),), assistant().origin, "stop"))],
    [ProviderError("server", "upstream failed", False)],
]


async def test_failed_checkpoints_leave_the_context_and_three_pause_auto_compaction_until_manual():
    store = InMemoryTranscriptStore()
    model = Model(reads(7), FAILURES)
    agent = make_agent(model, store=store, tools=[sized_reader(6_000, 6_000, 6_000, 2_000, 500, 500, 500)])
    events = await run(agent)
    # Requests 5, 6 and 7 each cross T, try once, fail, and are sent unchanged; request 8 is paused.
    assert len(model.checkpoint_requests) == 3 and len(model.conversation_requests) == 8
    assert not [row for row in await store.load("session") if row.kind == "compaction"]
    assert [event.error.split(":")[0] for event in events if isinstance(event, CompactionFailed)] == [
        "The checkpoint turn stopped with length.",
        "The checkpoint turn ended without checkpoint text.",
        "server",
    ]
    assert [event.cause for event in events if isinstance(event, CompactionPaused)] == ["failures"]
    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed"] * 3

    # Durable: a new Agent over the same rows stays paused and sends no checkpoint request.
    model = Model([call("read", "again", path="f"), [Done(assistant("ok"))]])
    resumed = make_agent(model, store=store, tools=[reader(tokens(1))])
    events = await run(resumed, row="next")
    assert not model.checkpoint_requests and not [e for e in events if isinstance(e, CompactionPaused)]

    # Manual /compact clears the pause, and its success resets the counters.
    model = Model([], [[Done(assistant(CHECKPOINT))]])
    manual = make_agent(model, store=store, tools=[reader(tokens(1))])
    events = [event async for event in manual.compact(turn_id="compact", focus="the parser")]
    assert model.checkpoint_requests[0].messages[-1].content[0].text.endswith(
        "Additional focus from the user: the parser\n</context-checkpoint-request>"
    )
    assert [type(event).__name__ for event in events if "Compaction" in type(event).__name__] == [
        "CompactionStarted",
        "CompactionFinished",
    ]
    state = [row for row in await store.load("session") if row.kind == "agent_state"][-1]
    assert "context" not in state.payload  # back to the default: not paused, no failures


async def test_three_ineffective_checkpoints_pause_auto_compaction():
    bloated = [[Done(assistant(tokens(15_000)))] for _ in range(3)]
    model = Model(reads(7), bloated)
    agent = make_agent(model, tools=[sized_reader(6_000, 6_000, 6_000, 6_000, 5_000, 5_000, 5_000)])
    events = await run(agent)
    finished = [event for event in events if isinstance(event, CompactionFinished)]
    assert len(finished) == 3 and all(event.tokens_after_estimate >= 0.75 * THRESHOLD for event in finished)
    assert [event.cause for event in events if isinstance(event, CompactionPaused)] == ["ineffective"]
    assert len(model.checkpoint_requests) == 3  # the fourth crossing is skipped


OVERFLOW = ProviderError("overflow", "prompt is too long: 40000 tokens > 32000 maximum", False, status=400)


async def test_a_provider_overflow_below_T_takes_a_normal_checkpoint_and_retries():
    model = Model(
        [call("read", "r0", path="f"), call("read", "r1", path="f"), [OVERFLOW], [Done(assistant("done"))]],
        [[Done(assistant(CHECKPOINT))]],
    )
    agent = make_agent(model, tools=[reader(tokens(4_000))])
    events = await run(agent)
    assert [is_checkpoint(request) for request in model.requests] == [False, False, False, True, False]
    rows = await agent.store.load("session")
    compaction = next(row for row in rows if row.kind == "compaction")
    assert (compaction.payload["mode"], compaction.payload["reason"]) == ("normal", "overflow")
    assert model.requests[-1].messages == project(rows[:-1]).messages
    assert events[-1].reason == "completed"


def _long_context(count: int):
    return [call("read", f"r{index}", path="f") for index in range(count)]


#: A 200,000-token window, where 45,000 tokens of history are well below T.
LARGE = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=200_000))


async def _history_on_a_large_window(store, sizes, *, row="before"):
    model = Model(reads(len(sizes), final="noted"))
    agent = make_agent(model, store=store, tools=[sized_reader(*sizes)], selection=LARGE)
    events = await run(agent, row=row)
    assert not model.checkpoint_requests and events[-1].reason == "completed"


async def test_the_ladder_rolls_when_no_fork_can_take_the_whole_context():
    # The route moves to the 32,000-token window with 45,000 tokens of history: a smaller window than expected.
    store = InMemoryTranscriptStore()
    await _history_on_a_large_window(store, [5_000] * 9)
    before = await store.load("session")
    model = Model([[Done(assistant("done"))]], [[Done(assistant("ROLL ONE"))], [Done(assistant("ROLL TWO"))]])
    agent = make_agent(model, store=store)
    events = await run(agent)
    first, second = model.checkpoint_requests
    request = model.conversation_requests[-1]
    rows = await store.load("session")
    compactions = [row for row in rows if row.kind == "compaction"]
    assert [(row.payload["mode"], row.payload["reason"]) for row in compactions] == [("rolling", "overflow")] * 2
    # (b): the first fork carries only the earliest part, a prefix of the request that did not fit.
    whole = project(rows[: compactions[0].context_seq - 1]).messages
    assert first.messages[:-1] == whole[: len(first.messages) - 1] and len(first.messages) - 1 < len(whole)
    assert abs(request_tokens("", (), first.messages[:-1]) - request_tokens("", (), whole) / 2) < 5_100
    assert compactions[0].payload["first_kept_seq"] > before[0].context_seq
    # Rolling again: the second fork's prefix starts with the first checkpoint.
    assert "ROLL ONE" in second.messages[0].content[0].text
    assert "ROLL TWO" in request.messages[0].content[0].text
    assert rows[-1].kind == "response" and request.messages == project(rows[:-1]).messages
    assert request_tokens(request.system, request.tools, request.messages) + 4_096 + 8_000 <= 32_000
    assert events[-1].reason == "completed"


async def test_the_ladder_drops_mechanically_once_a_checkpoint_request_fails():
    store = InMemoryTranscriptStore()
    await _history_on_a_large_window(store, [5_000] * 12)
    model = Model([], [[Done(assistant(CHECKPOINT))]])
    manual = make_agent(model, store=store, selection=LARGE)
    assert [event async for event in manual.compact(turn_id="compact")][-1].reason == "completed"
    await _history_on_a_large_window(store, [5_000] * 4, row="more")

    model = Model([[Done(assistant("done"))]], [[ProviderError("server", "down", False)]])
    agent = make_agent(model, store=store)
    events = await run(agent)
    assert len(model.checkpoint_requests) == 1  # the rolling request; after it fails, no model is called
    assert [event.reason for event in events if isinstance(event, CompactionFailed)] == ["overflow"]
    manual_row, *drops = [row for row in await store.load("session") if row.kind == "compaction"]
    finished = [(event.reason, event.mode) for event in events if isinstance(event, CompactionFinished)]
    assert drops and finished == [("overflow", "dropped")] * len(drops)
    for dropped in drops:
        assert dropped.payload["mode"] == "dropped" and dropped.payload["summarizer"] is None
        # The older checkpoint is kept, with its framing; only the earliest verbatim part moved out.
        assert dropped.payload["checkpoint"] == CHECKPOINT
        assert dropped.payload["summary"].startswith("<context-checkpoint>\nThis is a record")
        assert dropped.payload["first_kept_seq"] > manual_row.payload["first_kept_seq"]
    assert model.conversation_requests[-1].messages[0].content[0].text == drops[-1].payload["summary"]
    assert events[-1].reason == "completed"


async def test_the_ladder_stops_with_what_fills_the_context_when_nothing_more_can_move():
    # One result of 30,000 tokens: only the current request and that tool batch are left, and they do not fit.
    model = Model([call("read", "huge", path="f")])
    agent = make_agent(model, tools=[reader(tokens(30_000))])
    events = await run(agent)
    exhausted = next(event for event in events if isinstance(event, ContextExhausted))
    assert exhausted.limit == 32_000
    parts = {part.name: part.tokens for part in exhausted.parts}
    assert set(parts) == {"system", "tools", "history", "latest_tool_batch", "output"}
    assert parts["latest_tool_batch"] > 30_000 and parts["output"] == 4_096 + 8_000
    assert [(e.kind) for e in events if isinstance(e, AgentError)] == ["context_exhausted"]
    assert events[-1].reason == "context_exhausted"
    assert not model.checkpoint_requests  # nothing to summarize, so no model call


async def test_a_provider_that_always_overflows_stops_after_two_checkpoint_calls():
    def overflow(request):
        return [OVERFLOW]

    model = Model([*_long_context(8), *[overflow] * 10], [overflow] * 10)
    agent = make_agent(model, tools=[reader(tokens(1_500))])
    events = await run(agent)
    assert len(model.checkpoint_requests) == 2
    assert len(model.conversation_requests) == 8 + 4  # the reads, then the request and its three retries
    assert events[-1].reason == "context_exhausted"
    modes = [row.payload["mode"] for row in await agent.store.load("session") if row.kind == "compaction"]
    assert set(modes) == {"dropped"}


async def test_one_compaction_is_in_flight_per_session():
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        yield Done(assistant(CHECKPOINT))

    class SlowModel(Model):
        async def stream(self, request, cancel):
            self.requests.append(deepcopy(request))
            if is_checkpoint(request):
                async for event in slow(request):
                    yield event
                return
            for event in self.conversation.popleft():
                yield event

    model = SlowModel(history())
    agent = make_agent(model, tools=[reader(tokens(3_000))])
    await run(agent)
    compaction = agent.compact(turn_id="compact")
    task = asyncio.create_task(asyncio.wait_for(anext(compaction), 5))
    first = await task
    pending = asyncio.create_task(anext(compaction))
    await entered.wait()
    with pytest.raises(RuntimeError, match="active run"):
        await anext(agent.run(input_row("other", "hi"), turn_id="other"))
    with pytest.raises(RuntimeError, match="active run"):
        await anext(agent.compact(turn_id="again"))
    assert await agent.steer(input_row("steer", "no")) is False
    release.set()
    events = [first, await pending] + [event async for event in compaction]
    assert events[-1].reason == "completed"
    assert len(model.checkpoint_requests) == 1


async def test_clearing_runs_when_the_cache_is_cold_and_spares_recent_turns():
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    context = ContextConfig(clock=lambda: now[0])
    script = [*_long_context(9), [Done(assistant("one"))], [Done(assistant("two"))], [Done(assistant("three"))]]
    model = Model(script)
    agent = make_agent(model, store=store, tools=[reader(tokens(6_000))], selection=LARGE, context=context)
    await run(agent, row="turn-1")
    await run(agent, row="turn-2")
    assert not [row for row in await store.load("session") if row.kind == "context_edit"]  # warm, below 0.8 T
    results = deepcopy([row for row in await store.load("session") if row.kind == "tool_result"])
    now[0] += 301  # idle past the cache TTL
    await run(agent, row="turn-3")
    rows = await store.load("session")
    edits = [row for row in rows if row.kind == "context_edit"]
    by_id = {row.row_id: row.message.tool_call_id for row in results}
    # Nine results before the last two turns; the newest five eligible stay, the four older ones go.
    assert sorted(by_id[row.payload["target_event_id"]] for row in edits) == ["r0", "r1", "r2", "r3"]
    assert [row for row in rows if row.kind == "tool_result"] == results  # the originals are never rewritten
    sent = [m for m in model.requests[-1].messages if isinstance(m, ToolResultMessage)]
    assert [m.content[0].text.startswith("[Old tool result cleared") for m in sent] == [True] * 4 + [False] * 5
    assert model.requests[-1].messages == project(rows[:-1]).messages


async def test_chinese_output_reaches_the_threshold_by_its_utf8_size():
    chinese = "压缩" * 3_000  # 6,000 characters, 18,000 bytes: 4,500 tokens, where characters / 4 says 1,500
    model = Model(growing(5), [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(chinese)])
    await run(agent)
    crossing = next(index for index, request in enumerate(model.requests) if is_checkpoint(request))
    assert crossing == 5  # five results of 4,500 tokens cross 19,904; at characters / 4 none would


async def test_a_long_session_keeps_every_conversation_request_under_T():
    model = Model(
        [*[call("read", f"r{index}", path=f"f{index % 7}") for index in range(60)], [Done(assistant("done"))]],
        [[Done(assistant(f"{CHECKPOINT} {index}"))] for index in range(60)],
    )
    agent = make_agent(model, tools=[reader(tokens(2_500))])
    events = await run(agent)
    assert events[-1].reason == "completed"
    for request in model.conversation_requests:
        assert request_tokens(request.system, request.tools, request.messages) < THRESHOLD
    rows = await agent.store.load("session")
    compactions = [row for row in rows if row.kind == "compaction"]
    assert len(compactions) >= 3
    # Iterative: each later fork's prefix starts with the previous checkpoint, and file lists accumulate.
    assert all(
        request.messages[0].content[0].text.startswith("<context-checkpoint>")
        for request in model.checkpoint_requests[1:]
    )
    assert compactions[-1].payload["files_read"] == [f"f{index}" for index in range(7)]
    assert all(row.payload["previous_compaction_id"] == prior.row_id for prior, row in zip(compactions, compactions[1:]))
    # A fork anchored before the first checkpoint projects the original context.
    original = project(rows, fork_point=compactions[0].context_seq - 1).messages
    assert len(original) > len(project(rows).messages)
