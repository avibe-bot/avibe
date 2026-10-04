"""C-9 through the loop, observed at the recording provider and the store.

These cover what the pure rules in ``test_context.py`` cannot: when the loop
applies them, what the forked checkpoint request carries, what the checkpoint
turn may execute, how the overflow ladder steps, and the guards. The window is
the fakes' 32,000 tokens with 4,096 output, so T = 19,904 and keep = 4,976.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque
from copy import deepcopy
from dataclasses import replace

import pytest

from core.agent_core.agent.checkpoint import BUDGET_USED, DENIED, CheckpointPolicy, Decision
from core.agent_core.agent.scratch import ScratchRoot
from core.agent_core.agent.events import (
    AgentError,
    AssistantTextDelta,
    CompactionFailed,
    CompactionFinished,
    ContextExhausted,
    MessageCommitted,
    RunEnded,
    ToolStarted,
)
from core.agent_core.agent.hooks import Hooks
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import RetryPolicy
from core.agent_core.ai._common import endpoint_origin, prepare_messages
from core.agent_core.ai.anthropic import build_messages_payload
from core.agent_core.ai.openai_chat import build_chat_payload
from core.agent_core.ai.openai_responses import build_responses_payload
from core.agent_core.ai.provider import Done, ModelEndpoint, ProviderError, TextDelta
from core.agent_core.agent.models import ModelSelection
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.context import (
    CHECKPOINT_REQUEST,
    ContextConfig,
    StateRequest,
    message_tokens,
    request_tokens,
    state_cap,
)
from core.agent_core.harness.projection import project
from core.agent_core.messages import (
    AssistantMessage,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    text,
)
from core.agent_core.tools.base import ToolResult
from core.agent_core.tools.write import WriteTool
from tests.agent_core.fakes import (
    ENDPOINT,
    SELECTION as FAKE_SELECTION,
    FakeJobHost,
    FakeModelRouter,
    FakeTool,
    InMemoryTranscriptStore,
    assistant,
    input_row,
)

#: The engine tests' route: a 64,000-token window read through a 32,000-token input limit, so M = 8,000 and
#: T = 19,904 (section 1), the numbers every test here is sized against.
SELECTION = ModelSelection(ENDPOINT, replace(FAKE_SELECTION.capabilities, context_window=64_000, input_limit=32_000))
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
        return [f"<state skills={len(request.skills)}/>"]


def make_agent(
    model, *, store=None, tools=(), hooks=(), selection=SELECTION, context=None, cwd="/test-owned", models=None,
    **options,
):
    selections = selection if isinstance(selection, tuple) else (selection,)
    return Agent(
        session_id="session",
        models=models or FakeModelRouter(model, selections),
        tools=tools,
        hooks=hooks,
        store=store or InMemoryTranscriptStore(),
        jobs=FakeJobHost(),
        cwd=cwd,
        context=context or ContextConfig(),
        **options,
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
    """A first run whose context is past ``keep``, so a checkpoint has something to summarize."""
    return [call("read", "h0", path="old"), call("read", "h1", path="old"), [Done(assistant("first"))]]


#: A route whose input limit is well above ``0.9 * W``, so ``T`` is ``0.9 * W`` = 28,800 and a threshold
#: checkpoint there still leaves its turn ~67,000 tokens of room for tools (section 6).
ROOMY = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=32_000, input_limit=100_000))


def after_checkpoint():
    """``history()`` and the reply to the request that crosses ``T`` (``cross``), whether its checkpoint succeeds."""
    return [*history(), [Done(assistant("after"))]]


async def cross(agent, row="next"):
    """A Turn whose first request crosses ``T`` on ``ROOMY``: a threshold checkpoint, then the request goes out."""
    return await run(agent, tokens(23_000), row=row)


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
    # The state is capped for the route the next request goes to.
    assert host.states == [StateRequest("session", (), state_cap(SELECTION.capabilities))]
    assert host.records == [("session", compaction.payload["summarized_to_seq"])]

    # The next request: the checkpoint message (summary, then state), then the kept tool batch.
    checkpoint, *tail = after.messages
    summary, state = (block.text for block in checkpoint.content)
    assert CHECKPOINT in summary and "<current-request>\ngo\n</current-request>" in summary
    assert "lookup session through" in summary and state == "<state skills=0/>"
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
        ToolCallBlock("write-in", "write", {"path": "scratch/session/plan.md", "content": "a\n"}),
        ToolCallBlock("edit-in", "edit", {"path": str(scratch / "plan.md"), "oldText": "a", "newText": "b"}),
        ToolCallBlock("write-out", "write", {"path": str(outside / "f"), "content": "no"}),
        ToolCallBlock("write-up", "write", {"path": "scratch/session/../../outside/f", "content": "no"}),
        ToolCallBlock("write-link", "write", {"path": "scratch/session/escape/f", "content": "no"}),
        ToolCallBlock("edit-out", "edit", {"path": "notes.md", "edits": [{"oldText": "a", "newText": "b"}]}),
        ToolCallBlock("other", "other", {}),
    ]
    model = Model(
        after_checkpoint(),
        [[Done(AssistantMessage(tuple(calls), assistant().origin, "tool_use"))], [Done(assistant(CHECKPOINT))]],
    )
    context = ContextConfig(scratch_dir=str(scratch))
    agent = make_agent(model, tools=list(tools.values()), context=context, cwd=cwd, selection=ROOMY)
    await run(agent)
    before = await agent.store.load("session")
    events = await cross(agent)

    executed = {name: [ctx.tool_call_id for _, ctx in tool.calls] for name, tool in tools.items()}
    # Scratch writes and edits go through the root's descriptor, never through a tool or a pathname.
    assert executed == {"read": ["h0", "h1", "read"], "write": [], "edit": [], "bash": [], "other": []}
    assert (scratch / "plan.md").read_text() == "b\n" and not os.listdir(outside)
    results = {message.tool_call_id: message for message in model.checkpoint_requests[-1].messages[-len(calls) :]}
    denied = {key for key, message in results.items() if message.content[0].text == DENIED}
    assert denied == {"bash", "write-out", "write-up", "write-link", "edit-out", "other"}
    assert all(results[key].is_error for key in denied)
    # The turn's own calls and results are audit only: the context has none of them.
    rows = await agent.store.load("session")
    assert rows[: len(before)] == before
    # The crossing input stays as the tail, so this checkpoint counts as ineffective: its guard commits with it.
    assert [row.kind for row in rows[len(before) :]] == ["input", "compaction", "response"]
    assert not [event for event in events if isinstance(event, ToolStarted)]
    assert len(agent.store.audits[0][2]["messages"]) == 2 + len(calls)
    assert events[-1].reason == "completed"


@pytest.mark.parametrize("malformed", ["duplicate call ids", "arguments that are not JSON"])
async def test_a_malformed_checkpoint_response_fails_the_checkpoint_before_any_call_runs(malformed):
    # Admission is one path for every response (invariant 6): the checkpoint turn's are checked before any call.
    if malformed == "duplicate call ids":
        calls = (ToolCallBlock("same", "read", {"path": "a"}), ToolCallBlock("same", "read", {"path": "b"}))
    else:
        calls = (ToolCallBlock("odd", "read", {"path": "a"}),)
        calls[0].arguments["path"] = float("nan")  # valid when built, changed after: the arguments are mutable
    read = reader(tokens(3_000))
    model = Model(after_checkpoint(), [[Done(AssistantMessage(calls, assistant().origin, "tool_use"))]])
    agent = make_agent(model, tools=[read], selection=ROOMY)
    await run(agent)
    before = await agent.store.load("session")
    events = await cross(agent)

    assert [ctx.tool_call_id for _, ctx in read.calls] == ["h0", "h1"]  # no call of the checkpoint turn ran
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert len(failed) == 1 and failed[0].error.startswith("Provider protocol violation")
    rows = await agent.store.load("session")
    assert rows[: len(before)] == before and [row.kind for row in rows[len(before) :]] == ["input", "response"]
    audit = agent.store.audits[-1][2]
    assert audit["outcome"] == "failed" and audit["error"] == failed[0].error
    # The audit keeps the refused response when JSON can hold it.
    assert len(audit["messages"]) == (1 if malformed == "duplicate call ids" else 0)


async def test_a_target_the_platform_cannot_compare_with_the_scratch_dir_is_denied(tmp_path, monkeypatch):
    # On Windows, commonpath raises ValueError for paths on different drives (D:\\notes.txt vs C:\\scratch).
    def other_drive(paths):
        raise ValueError("Paths don't have the same drive")

    policy = CheckpointPolicy(cwd=str(tmp_path), scratch_dir=str(tmp_path / "scratch"))
    monkeypatch.setattr("core.agent_core.agent.checkpoint.os.path.commonpath", other_drive)
    write = ToolCallBlock("w", "write", {"path": str(tmp_path / "elsewhere"), "content": "no"})
    assert policy.decide(write) == Decision(denial=DENIED)


async def test_the_checkpoint_turn_gets_five_tool_rounds_then_must_write():
    tools = [reader(tokens(3_000)), FakeTool("bash")]
    rounds = [call("read", f"r{index}", path="f") for index in range(5)]
    sixth = [ToolCallBlock("r5", "read", {"path": "f"}), ToolCallBlock("b5", "bash", {"command": "ls"})]
    rounds.append([Done(AssistantMessage(tuple(sixth), assistant().origin, "tool_use"))])
    rounds.append(call("read", "r6", path="f"))
    model = Model(after_checkpoint(), rounds)
    agent = make_agent(model, tools=tools, selection=ROOMY)
    await run(agent)
    before = await agent.store.load("session")
    tools[0].result = ToolResult((text("small"),))
    events = await cross(agent)
    assert [ctx.tool_call_id for _, ctx in tools[0].calls] == ["h0", "h1", "r0", "r1", "r2", "r3", "r4"]
    # Once the budget is used up, every call is told so, whatever the table says about it.
    answered = [
        (message.tool_call_id, message.content[0].text) for message in model.checkpoint_requests[-1].messages[-2:]
    ]
    assert answered == [("r5", BUDGET_USED), ("b5", BUDGET_USED)]
    assert not tools[1].calls
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert len(failed) == 1 and "kept calling tools" in failed[0].error
    after = await agent.store.load("session")
    assert after[: len(before)] == before and [row.kind for row in after[len(before) :]] == ["input", "response"]
    assert events[-1].reason == "completed"  # the request itself still went out


async def test_a_threshold_checkpoint_may_read_while_the_window_has_room_and_never_past_it():
    # Crossing T at ~20,000 tokens leaves ~7,100 tokens of room in the 32,000 window: the read runs, cut to fit.
    peek = [call("read", "peek", path="f"), [Done(assistant(CHECKPOINT))]]
    model = Model(reads(4), peek)
    tool = sized_reader(6_000, 6_000, 6_000, 2_000, 10_000)
    agent = make_agent(model, tools=[tool])
    await run(agent)
    assert [ctx.tool_call_id for _, ctx in tool.calls] == ["read-0", "read-1", "read-2", "read-3", "peek"]
    first, second = model.checkpoint_requests
    room = 32_000 - request_tokens(first.system, first.tools, first.messages) - 4_096
    room -= message_tokens(second.messages[-2])  # the response carrying the call
    result = second.messages[-1]
    assert result.content[-1].text.startswith("[Output truncated to fit this checkpoint turn: showing about")
    assert message_tokens(result) <= room - 1_000
    assert request_tokens(second.system, second.tools, second.messages) + 4_096 <= 32_000

    # Crossing T at ~24,000 tokens leaves under the 4,000-token floor: the read is denied and never runs.
    model = Model(reads(4), [call("read", "peek", path="f"), [Done(assistant(CHECKPOINT))]])
    tool = reader(tokens(6_000))
    agent = make_agent(model, tools=[tool])
    await run(agent)
    assert [ctx.tool_call_id for _, ctx in tool.calls] == ["read-0", "read-1", "read-2", "read-3"]
    assert model.checkpoint_requests[-1].messages[-1].content[0].text == BUDGET_USED

    # A response that leaves under 1,000 tokens of room still gets the policy's fixed text, never a cut of it:
    # a truncation note would tell the model to read again, and another call fails the turn.
    long = AssistantMessage(
        (ThinkingBlock(tokens(6_500)), ToolCallBlock("peek", "read", {"path": "f"})), assistant().origin, "tool_use"
    )
    model = Model(reads(4), [[Done(long)], [Done(assistant(CHECKPOINT))]])
    tool = sized_reader(6_000, 6_000, 6_000, 2_000)
    agent = make_agent(model, tools=[tool])
    events = await run(agent)
    first, second = model.checkpoint_requests
    room = 32_000 - request_tokens(first.system, first.tools, first.messages) - 4_096 - message_tokens(long)
    assert room < 1_000 + message_tokens(second.messages[-1])  # the bound would have cut it
    assert second.messages[-1].content == (text(BUDGET_USED),)
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["normal"]


async def test_a_checkpoint_turn_reserves_room_for_every_result_of_its_batch():
    # Sixty parallel reads at a threshold checkpoint: the first runs, the rest get the fixed text. Room for all of
    # those results is reserved before any call runs, so the next checkpoint request still fits and succeeds.
    calls = tuple(ToolCallBlock(f"c{index}", "read", {"path": f"f{index}"}) for index in range(60))
    batch = AssistantMessage(calls, assistant().origin, "tool_use")
    model = Model(reads(4), [[Done(batch)], [Done(assistant(CHECKPOINT))]])
    tool = sized_reader(6_000, 6_000, 6_000, 2_000, 9_000)
    agent = make_agent(model, tools=[tool])
    events = await run(agent)
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["normal"]
    second = model.checkpoint_requests[-1]
    results = [message for message in second.messages if isinstance(message, ToolResultMessage)][-60:]
    assert [message.tool_call_id for message in results] == [call.id for call in calls]
    assert sum(message.content[0].text == BUDGET_USED for message in results) >= 59
    assert request_tokens(second.system, second.tools, second.messages) + 4_096 <= 32_000


class FailingHost(Host):
    def __init__(self, step: str) -> None:
        super().__init__()
        self.step = step

    def earlier_record(self, session_id, through_seq):
        if self.step == "earlier_record":
            raise RuntimeError("lookup unavailable")
        return super().earlier_record(session_id, through_seq)

    async def render_state(self, request):
        if self.step == "render_state":
            raise RuntimeError("state store unavailable")
        return await super().render_state(request)


_TWIN_READS = (ToolCallBlock("same", "read", {"path": "a"}), ToolCallBlock("same", "read", {"path": "b"}))


@pytest.mark.parametrize("step", ["provider", "admission", "render_state", "earlier_record", "row", "commit"])
async def test_a_checkpoint_turn_is_finalized_once_whatever_step_fails(step, monkeypatch):
    # Invariant 4 for checkpoint turns: one audit on every exit. A failed request (provider, admission) or a host
    # failure after the model answered is a failed checkpoint, counted, and the request still goes out; an engine
    # error building the row or a store failure ends the run with nothing of the checkpoint landed (invariant 3),
    # and still leaves the audit. (A request that cannot be composed: the next test.)
    answers = {
        "provider": [ProviderError("server", "upstream failed", False)],
        "admission": [Done(AssistantMessage(_TWIN_READS, assistant().origin, "tool_use"))],
    }
    store = FailingTransactionStore(failures=0)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY))
    before = await store.load("session")
    if step == "row":
        def broken(*args, **kwargs):
            raise ValueError("engine bug")

        monkeypatch.setattr("core.agent_core.agent.loop.compaction_payload", broken)
    store.failures = 1 if step == "commit" else 0
    model = Model([[Done(assistant("after"))]], [answers.get(step, [Done(assistant(CHECKPOINT))])])
    agent = make_agent(
        model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=ContextConfig(host=FailingHost(step))
    )
    events = await cross(agent)

    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed"]
    # The turn's response from the ledger; a provider error with no partial has none.
    assert len(store.audits[0][2]["messages"]) == (0 if step == "provider" else 1)
    rows = await store.load("session")
    if step in ("row", "commit"):
        assert [row.kind for row in rows[len(before) :]] == ["input"] and events[-1].reason == "error"
    else:
        assert [row.kind for row in rows[len(before) :]] == ["input", "response"]
        failed = [event for event in events if isinstance(event, CompactionFailed)]
        assert len(failed) == 1 and store.audits[0][2]["error"] == failed[0].error


async def test_a_checkpoint_request_that_cannot_fit_a_new_route_is_audited_and_the_ladder_rolls():
    # The turn's second request resolves to an 8,000-token window, where its fork cannot fit: it is never sent, the
    # checkpoint fails as an overflow and is audited, and the ladder rolls on the conversation's own route.
    small = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=8_000, input_limit=None))
    store = InMemoryTranscriptStore()
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY))
    model = Model([[Done(assistant("after"))]], [call("read", "first", path="f"), [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=(ROOMY, small))
    events = await cross(agent)
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert len(failed) == 1 and failed[0].error.startswith("overflow: the checkpoint request does not fit")
    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed", "completed"]
    assert len(store.audits[0][2]["messages"]) == 2  # the read call and its result; the request never went
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["rolling"]
    assert events[-1].reason == "completed"


FAILURES = [
    [Done(assistant(CHECKPOINT, stop_reason="length"))],
    [Done(AssistantMessage((ThinkingBlock("only thinking"),), assistant().origin, "stop"))],
    [ProviderError("server", "upstream failed", False)],
]


@pytest.mark.parametrize(
    "response",
    [
        assistant(CHECKPOINT, stop_reason="tool_use"),  # a tool-use stop with no call is not a final answer
        assistant(CHECKPOINT, calls=[ToolCallBlock("c", "read", {"path": "f"})], stop_reason="stop"),
        assistant(CHECKPOINT, stop_reason="refusal"),
    ],
)
async def test_only_a_stop_with_text_and_no_calls_is_a_checkpoint(response):
    model = Model(after_checkpoint(), [[Done(response)]])
    tool = reader(tokens(3_000))
    agent = make_agent(model, tools=[tool], selection=ROOMY)
    await run(agent)
    events = await cross(agent)
    assert [type(event).__name__ for event in events if "Compaction" in type(event).__name__] == [
        "CompactionStarted",
        "CompactionFailed",
    ]
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]
    assert [ctx.tool_call_id for _, ctx in tool.calls] == ["h0", "h1"]  # the call was never run


async def test_the_checkpoint_usage_keeps_every_reported_field():
    def with_usage(message, reasoning):
        usage = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=5, reasoning_tokens=reasoning)
        return [Done(replace(message, usage=usage))]

    turn = [with_usage(assistant(calls=[ToolCallBlock("r", "read", {"path": "f"})]), 7), with_usage(assistant(CHECKPOINT), 3)]
    model = Model(after_checkpoint(), turn)
    agent = make_agent(model, tools=[reader(tokens(3_000))], selection=ROOMY)
    await run(agent)
    assert (await cross(agent))[-1].reason == "completed"
    expected = {"input_tokens": 200, "output_tokens": 20, "cache_read_tokens": 10, "cache_write_tokens": 0, "reasoning_tokens": 10}
    compaction = next(row for row in await agent.store.load("session") if row.kind == "compaction")
    assert compaction.payload["usage"] == expected
    assert agent.store.audits[0][2]["usage"] == expected


async def test_failing_checkpoints_stop_after_two_in_a_run_and_the_next_run_tries_again():
    store = InMemoryTranscriptStore()
    model = Model(reads(7), FAILURES[:2])
    agent = make_agent(model, store=store, tools=[sized_reader(6_000, 6_000, 6_000, 2_000, 500, 500, 500)])
    events = await run(agent)
    # Requests 5 and 6 each cross T, try once, fail, and are sent unchanged; 7 and 8 cross it and are sent untried.
    assert len(model.checkpoint_requests) == 2 and len(model.conversation_requests) == 8
    assert not [row for row in await store.load("session") if row.kind == "compaction"]
    assert [event.error.split(":")[0] for event in events if isinstance(event, CompactionFailed)] == [
        "The checkpoint turn stopped with length and no usable checkpoint.",
        "The checkpoint turn ended without checkpoint text.",
    ]
    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed"] * 2
    assert events[-1].reason == "completed"
    # Nothing is kept: the next run tries again.
    model.conversation.extend([call("read", "again", path="f"), [Done(assistant("ok"))]])
    model.checkpoint.append([Done(assistant(CHECKPOINT))])
    events = await run(agent, row="next")
    assert len(model.checkpoint_requests) == 3 and events[-1].reason == "completed"
    assert [row.kind for row in await store.load("session")].count("compaction") == 1


class _UnusableHost(Host):
    async def render_state(self, request):
        self.states.append(request)
        raise RuntimeError("the state store is down")


async def test_once_a_run_stops_compacting_a_request_that_cannot_fit_ends_it_and_nothing_is_built():
    model, host = Model(reads(6), FAILURES[:2]), _UnusableHost()
    agent = make_agent(
        model, context=ContextConfig(host=host), tools=[sized_reader(6_000, 6_000, 6_000, 2_000, 500, 12_000)]
    )
    events = await run(agent)
    # Two failed checkpoints stop compaction for the run; the request a 12,000-token result leaves cannot fit.
    assert len(model.checkpoint_requests) == 2
    assert not host.states  # no checkpoint row, not even the stop check's dry run, is built
    assert events[-1].reason == "context_exhausted" and [e for e in events if isinstance(e, ContextExhausted)]
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]


async def test_the_second_unproductive_checkpoint_of_a_run_ends_a_request_that_cannot_fit_without_a_drop():
    model = Model(reads(5), FAILURES[:2])
    agent = make_agent(model, tools=[sized_reader(6_000, 6_000, 6_000, 2_000, 9_000)])
    events = await run(agent)
    # Request 5 crosses T and its checkpoint fails (one); request 6 cannot fit, its checkpoint fails too (two),
    # so the run stops compacting: the request ends the run, never in a mechanical drop.
    assert len(model.checkpoint_requests) == 2
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]
    assert events[-1].reason == "context_exhausted"


async def test_a_checkpoint_on_a_fallback_renders_its_state_for_the_conversation_route():
    # The checkpoint's first attempt is refused on the primary and retried on a fallback with a far larger window;
    # its state is still capped for the route the conversation's next request is composed for.
    other = ModelEndpoint("anthropic", "http://model.invalid", "other-model", "", provider="test-provider")
    router = None

    def busy(request):
        router.retrying = True
        return [ProviderError("rate_limit", "busy", True)]

    def answers(request):
        router.retrying = False
        assert request.endpoint.model_id == "other-model"
        return [Done(assistant(CHECKPOINT))]

    host = Host()
    model = Model(after_checkpoint(), [busy, answers])
    fallback = ModelSelection(other, replace(ROOMY.capabilities, context_window=1_000_000))
    router = _FallbackOnRetry(model, ROOMY, fallback)
    agent = make_agent(model, models=router, context=ContextConfig(host=host), tools=[reader(tokens(3_000))],
                       retry=RetryPolicy(initial_delay_s=0))
    await run(agent)
    assert (await cross(agent))[-1].reason == "completed"
    assert [request.cap for request in host.states] == [state_cap(ROOMY.capabilities)]


class _FallbackOnRetry(FakeModelRouter):
    """The primary route; while ``retrying`` is set, a retry resolves to the fallback."""

    def __init__(self, model, primary, fallback) -> None:
        super().__init__(model, (primary,))
        self.fallback, self.retrying = fallback, False

    async def resolve(self):
        self.resolutions += 1
        return self.fallback if self.retrying else self.selections[0]


async def test_a_failed_checkpoint_attempt_keeps_every_partial_in_its_audit():
    retried = assistant("", stop_reason="error")
    retried = replace(retried, usage=Usage(input_tokens=50, output_tokens=0))
    streamed = replace(assistant("half a checkpoint", stop_reason="error"), usage=Usage(input_tokens=70, output_tokens=9))
    turn = [
        [ProviderError("server", "upstream reset", True, partial=retried)],
        [ProviderError("server", "upstream failed", False, partial=streamed)],
    ]
    model = Model(after_checkpoint(), turn)
    agent = make_agent(model, tools=[reader(tokens(3_000))], retry=RetryPolicy(initial_delay_s=0), selection=ROOMY)
    await run(agent)
    assert (await cross(agent))[-1].reason == "completed"  # the checkpoint failed; the request itself went out
    (_, _, audit), = agent.store.audits
    assert audit["outcome"] == "failed"
    assert [message["content"] for message in audit["messages"]] == [[], [{"type": "text", "text": "half a checkpoint"}]]
    assert audit["usage"] == {"input_tokens": 120, "output_tokens": 9, "cache_read_tokens": 0, "cache_write_tokens": 0}


async def test_a_checkpoint_turn_measures_each_request_on_the_route_resolved_for_it():
    # The route resolves again for the turn's second request and lands on a 35,000-token window with 2,048 output:
    # the fork still fits there, but with under 4,000 tokens of room.
    small = ModelSelection(
        ENDPOINT, replace(SELECTION.capabilities, context_window=35_000, input_limit=None, max_output_tokens=2_048)
    )
    store = InMemoryTranscriptStore()
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY))
    tool = reader("small")
    script = [call("read", "first", path="f"), call("read", "second", path="f"), [Done(assistant(CHECKPOINT))]]
    model = Model([[Done(assistant("after"))]], script)
    agent = make_agent(model, store=store, tools=[tool], selection=(ROOMY, small))
    assert (await cross(agent))[-1].reason == "completed"
    first, second, third = model.checkpoint_requests
    assert (first.max_tokens, second.max_tokens) == (4_096, 2_048)
    # On the small window the second read no longer fits the floor: denied, never run.
    assert [ctx.tool_call_id for _, ctx in tool.calls] == ["first"]
    assert third.messages[-1].content[0].text == BUDGET_USED


def billed(message: AssistantMessage, ratio: float = 1.0):
    """A script answering with ``message``, billed ``ratio`` x the request's UTF-8/4 size (a tokenizer)."""

    def script(request):
        size = request_tokens(request.system, request.tools, request.messages)
        return [Done(replace(message, usage=Usage(input_tokens=int(size * ratio), output_tokens=10)))]

    return script


ENGLISH = "The quick brown fox jumps over the lazy dog. " * 4


def english(count: int) -> str:
    """About ``count`` UTF-8/4 tokens of English, which a real tokenizer counts about 20% lower."""
    return (ENGLISH * (count * 4 // len(ENGLISH) + 1))[: count * 4]


async def _english_turn(store, *, context=True):
    read = billed(assistant(calls=[ToolCallBlock("r", "read", {"path": "notes.md"})]), 0.8)
    model = Model([read, billed(assistant("Summary of the notes."), 0.8)])
    agent = Agent(
        session_id="session",
        models=FakeModelRouter(model),
        tools=[reader(english(18_000))],
        hooks=(),
        store=store,
        jobs=FakeJobHost(),
        cwd="/test-owned",
        context=ContextConfig() if context else None,
    )
    await run(agent, english(100), row="first")


async def test_a_new_turn_on_a_large_english_conversation_uses_the_stored_anchor_and_does_not_compact_early():
    # The second Turn's request is about 20,300 UTF-8/4 tokens, past T, but the last response's real usage
    # (80% of the bytes) plus what came after it is about 16,900: no checkpoint.
    store = InMemoryTranscriptStore()
    await _english_turn(store)
    model = Model([[Done(assistant("ok"))]], [[Done(assistant(CHECKPOINT))]])
    events = await run(make_agent(model, store=store), english(2_000), row="second")
    assert not model.checkpoint_requests and events[-1].reason == "completed"
    request = model.conversation_requests[0]
    assert request_tokens(request.system, request.tools, request.messages) >= THRESHOLD

    # The same conversation written without request facts has no anchor: the bytes say compact.
    store = InMemoryTranscriptStore()
    await _english_turn(store, context=False)
    model = Model([[Done(assistant("ok"))]], [[Done(assistant(CHECKPOINT))]])
    await run(make_agent(model, store=store), english(2_000), row="second")
    assert len(model.checkpoint_requests) == 1


async def test_the_request_right_after_a_checkpoint_is_measured_and_rolls_when_it_does_not_fit():
    big = [[Done(assistant(tokens(16_000)))], [Done(assistant("short"))]]
    model = Model(reads(6), big)
    agent = make_agent(model, tools=[sized_reader(5_000, 5_000, 5_000, 2_000, 2_000, 2_000)])
    events = await run(agent)
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["normal", "rolling"]
    for request in model.conversation_requests:
        assert request_tokens(request.system, request.tools, request.messages) + 4_096 + 8_000 <= 32_000


async def test_two_ineffective_checkpoints_stop_compaction_for_the_run():
    bloated = [[Done(assistant(tokens(15_000)))] for _ in range(2)]
    model = Model(reads(7), bloated)
    agent = make_agent(model, tools=[sized_reader(6_000, 6_000, 6_000, 6_000, 5_000, 5_000, 5_000)])
    events = await run(agent)
    finished = [event for event in events if isinstance(event, CompactionFinished)]
    assert len(finished) == 2 and all(event.tokens_after_estimate >= 0.75 * THRESHOLD for event in finished)
    assert len(model.checkpoint_requests) == 2  # the next crossing is not tried in this run


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
LARGE = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=200_000, input_limit=None))


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
    # On a 70,000-token window the 12 results cross T: a normal checkpoint, the one the drops later keep.
    medium = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=70_000, input_limit=None))
    first = make_agent(Model([[Done(assistant("noted"))]], [[Done(assistant(CHECKPOINT))]]), store=store, selection=medium)
    assert (await run(first, row="first"))[-1].reason == "completed"
    await _history_on_a_large_window(store, [5_000] * 4, row="more")

    model = Model([[Done(assistant("done"))]], [[ProviderError("server", "down", False)]])
    agent = make_agent(model, store=store)
    events = await run(agent)
    assert len(model.checkpoint_requests) == 1  # the rolling request; after it fails, no model is called
    assert [event.reason for event in events if isinstance(event, CompactionFailed)] == ["overflow"]
    first_row, *drops = [row for row in await store.load("session") if row.kind == "compaction"]
    finished = [(event.reason, event.mode) for event in events if isinstance(event, CompactionFinished)]
    assert drops and finished == [("overflow", "dropped")] * len(drops)
    for dropped in drops:
        assert dropped.payload["mode"] == "dropped" and dropped.payload["summarizer"] is None
        # The older checkpoint is kept, with its framing; only the earliest verbatim part moved out.
        assert dropped.payload["checkpoint"] == CHECKPOINT
        assert dropped.payload["summary"].startswith("<context-checkpoint>\nThis is a record")
        assert dropped.payload["first_kept_seq"] > first_row.payload["first_kept_seq"]
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
    assert len(model.requests) == 1  # the oversized request never reached the provider


async def test_the_single_unit_stop_budgets_the_request_that_would_remain():
    # The last response was billed far above its bytes (many short messages, protocol framing), so the anchored
    # estimate is past the window. Dropping the old units leaves a small request: the ladder moves on, no stop.
    store = InMemoryTranscriptStore()

    def billed_high(request):
        return [Done(replace(assistant("noted"), usage=Usage(input_tokens=30_000, output_tokens=10)))]

    first = Model([call("read", "r0", path="f"), call("read", "r1", path="f"), billed_high])
    await run(make_agent(first, store=store, tools=[reader(tokens(1_000))]), row="first")
    model = Model([[Done(assistant("done"))]], [[Done(assistant(CHECKPOINT))]])
    events = await run(make_agent(model, store=store), tokens(1_000), row="second")
    assert not [event for event in events if isinstance(event, ContextExhausted)]
    assert events[-1].reason == "completed"
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["rolling"]
    request = model.conversation_requests[0]  # the only one sent, after the checkpoint
    assert request_tokens(request.system, request.tools, request.messages) + 4_096 <= 32_000


async def test_the_stage_judges_the_fork_it_would_send_exactly_as_the_turn_composes_it():
    # Rehydrated state rides in every request, the fork's included: the stage's dry run counts it, so it never starts
    # a normal checkpoint whose first request the turn would then refuse; it goes straight to the rolling step.
    state = UserMessage((text(tokens(4_000)),))
    model = Model([*growing(3)[:2], call("read", "big", path="f"), [Done(assistant("done"))]], [[Done(assistant(CHECKPOINT))]] * 3)
    agent = make_agent(model, tools=[sized_reader(6_000, 6_000, 12_000)], rehydrate=lambda _: [state])
    events = await run(agent)
    assert not [event for event in events if isinstance(event, CompactionFailed)]
    assert [event.mode for event in events if isinstance(event, CompactionFinished)][0] == "rolling"
    assert events[-1].reason == "completed"
    assert all(request.messages[0] == state for request in model.requests)


async def test_the_stop_check_counts_the_split_turn_input_the_drop_would_copy():
    # The last unit is a tool batch after a large input: moving everything else out still copies that input into
    # <current-request>, so nothing can make the request fit. It stops before any checkpoint call or row.
    model = Model([call("read", "big", path="f")], [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(12_000))])
    events = await run(agent, tokens(18_000))
    assert events[-1].reason == "context_exhausted"
    assert [event for event in events if isinstance(event, ContextExhausted)]
    assert not model.checkpoint_requests
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]


async def test_the_stop_check_budgets_exactly_the_request_the_drop_leaves(monkeypatch):
    # One assembler of checkpoint + tail: the minimal request the stop check budgets is the request the conversation
    # sends once the drop has moved everything but the last unit out.
    seen = []
    minimal = Agent._minimal

    async def spy(self, *args, **kwargs):
        composed, plan = await minimal(self, *args, **kwargs)
        seen.append(composed)
        return composed, plan

    monkeypatch.setattr(Agent, "_minimal", spy)
    host = Host()
    failing = [Done(assistant(CHECKPOINT, stop_reason="length"))]  # the rolling checkpoint fails: the ladder drops
    model = Model([[Done(assistant("ok"))], call("read", "big", path="f"), [Done(assistant("done"))]], [failing])
    agent = make_agent(model, tools=[reader(tokens(16_000))], context=ContextConfig(host=host))
    await run(agent, tokens(12_000), row="first")
    events = await run(agent, "now read the big file", row="second")
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["dropped"]
    assert events[-1].reason == "completed"
    assert seen and seen[0] == model.conversation_requests[-1]


async def test_an_overflow_after_streamed_output_is_never_retried():
    # The adapter streamed text, then reported overflow without a partial: the user saw output, so no retry.
    model = Model([*history(), [TextDelta(0, "partial answer"), OVERFLOW]], [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(3_000))])
    await run(agent)
    events = await run(agent, row="second")
    assert events[-1].reason == "context_exhausted"
    assert not model.checkpoint_requests and len(model.conversation_requests) == 4


async def test_a_provider_that_always_overflows_stops_after_two_checkpoint_calls():
    def overflow(request):
        return [OVERFLOW]

    model = Model([*_long_context(8), *[overflow] * 10], [overflow] * 10)
    agent = make_agent(model, tools=[reader(tokens(1_500))])
    events = await run(agent)
    assert len(model.checkpoint_requests) == 2
    # The reads, then the refused request: its normal and rolling checkpoints are refused too, which stops the
    # run's compaction, so the refusal ends the run; nothing is dropped.
    assert len(model.conversation_requests) == 8 + 1
    assert events[-1].reason == "context_exhausted"
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]


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

    model = SlowModel(after_checkpoint())
    agent = make_agent(model, tools=[reader(tokens(3_000))], selection=ROOMY)
    await run(agent)
    crossing = asyncio.ensure_future(_drain(agent.run(input_row("next", tokens(23_000)), turn_id="next")))
    await entered.wait()
    # While the run's checkpoint turn is in flight, no other run starts, so no second compaction either.
    with pytest.raises(RuntimeError, match="active run"):
        await anext(agent.run(input_row("other", "hi"), turn_id="other"))
    release.set()
    events = await crossing
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
    assert store.transactions == [["context_edit"] * 4]  # one transition, one transaction


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
    assert compactions[-1].payload["files_read"] == [f"f{index}" for index in range(6, -1, -1)]  # most recent first
    assert all(row.payload["previous_compaction_id"] == prior.row_id for prior, row in zip(compactions, compactions[1:]))
    # A fork anchored before the first checkpoint projects the original context.
    original = project(rows, fork_point=compactions[0].context_seq - 1).messages
    assert len(original) > len(project(rows).messages)


# --- C-9 ordering and ownership invariants (context.md section 10) -----------------------------


def fits_the_window(request) -> bool:
    return request_tokens(request.system, request.tools, request.messages) + 4_096 + 8_000 <= 32_000


class FailingTransactionStore(InMemoryTranscriptStore):
    """Its next ``append_payloads`` transactions fail as a database would: nothing of them lands."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def append_payloads(self, session_id, entries):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transaction failed")
        return await super().append_payloads(session_id, entries)


async def test_invariant_3_a_c9_transition_commits_in_one_transaction_or_not_at_all():
    # A checkpoint's row commits in one transaction or not at all.
    store = FailingTransactionStore(failures=1)
    model = Model(reads(4), [[Done(assistant(tokens(15_000)))]])
    agent = make_agent(model, store=store, tools=[sized_reader(6_000, 6_000, 6_000, 6_000)])
    events = await run(agent)
    rows = await store.load("session")
    # Nothing of the failed transition is anywhere: no checkpoint, no announcement.
    assert not [row for row in rows if row.kind == "compaction"]
    assert [type(event).__name__ for event in events if "Compaction" in type(event).__name__] == ["CompactionStarted"]
    assert events[-1].reason == "error"
    model = Model(reads(1), [[Done(assistant(tokens(15_000)))]])
    agent = make_agent(model, store=store, tools=[sized_reader(1)])
    await run(agent, row="again")
    assert store.transactions[-1] == ["compaction"]




# --- round 5 decisions -------------------------------------------------------------------------------


def test_context_management_and_user_hooks_are_mutually_exclusive_in_v1():
    with pytest.raises(ValueError, match="mutually exclusive"):
        make_agent(Model(), hooks=[Hooks()])


@pytest.mark.parametrize("context", [True, False])
async def test_a_usage_only_partial_never_enters_the_context_and_the_retry_resends_the_request(context):
    billed = replace(assistant("", stop_reason="error"), usage=Usage(input_tokens=900, output_tokens=0))
    model = Model([[ProviderError("rate_limit", "busy", True, partial=billed)], [Done(assistant("done"))]])
    store = InMemoryTranscriptStore()
    agent = Agent(
        session_id="session",
        models=FakeModelRouter(model),
        tools=(),
        hooks=(),
        store=store,
        jobs=FakeJobHost(),
        cwd="/test-owned",
        retry=RetryPolicy(initial_delay_s=0),
        context=ContextConfig() if context else None,
    )
    events = await run(agent)
    assert events[-1].reason == "completed"
    first, second = model.requests
    assert second.messages == first.messages  # the original request, unchanged
    responses = [row.message for row in await store.load("session") if row.kind == "response"]
    assert all(response.content for response in responses)
    # In every mode, the attempt's billed usage is exactly one audit row, outside the context.
    assert [attempt["usage"]["input_tokens"] for _, _, attempt in store.attempts] == [900]


def flat_scratch(tmp_path):
    scratch = tmp_path / "state" / "scratch" / "session"
    outside = tmp_path / "outside"
    outside.mkdir()
    return scratch, outside


async def _scratch_checkpoint(tmp_path, scratch, calls, tools):
    model = Model(
        after_checkpoint(),
        [[Done(AssistantMessage(tuple(calls), assistant().origin, "tool_use"))], [Done(assistant(CHECKPOINT))]],
    )
    agent = make_agent(
        model,
        tools=[reader(tokens(3_000)), *tools],
        context=ContextConfig(scratch_dir=str(scratch)),
        cwd=str(tmp_path),
        selection=ROOMY,
    )
    await run(agent)
    events = await cross(agent)
    assert events[-1].reason == "completed"
    results = {m.tool_call_id: m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)}
    return results


async def test_scratch_writes_are_flat_inside_a_root_the_turn_creates(tmp_path):
    scratch, outside = flat_scratch(tmp_path)
    assert not scratch.exists()
    write = WriteTool()
    calls = [
        ToolCallBlock("flat", "write", {"path": "state/scratch/session/plan.md", "content": "plan"}),
        ToolCallBlock("nested", "write", {"path": "state/scratch/session/sub/plan.md", "content": "no"}),
    ]
    results = await _scratch_checkpoint(tmp_path, scratch, calls, [write])
    assert not results["flat"].is_error and (scratch / "plan.md").read_text() == "plan"
    # A subdirectory is never authorized, so no parent is ever created and no parent component can race.
    assert results["nested"].content[0].text == DENIED and not (scratch / "sub").exists()


async def test_a_scratch_root_that_is_a_symlink_authorizes_nothing(tmp_path):
    scratch, outside = flat_scratch(tmp_path)
    scratch.parent.mkdir(parents=True)
    scratch.symlink_to(outside, target_is_directory=True)
    calls = [ToolCallBlock("w", "write", {"path": "state/scratch/session/plan.md", "content": "no"})]
    results = await _scratch_checkpoint(tmp_path, scratch, calls, [WriteTool()])
    assert results["w"].content[0].text == DENIED and not os.listdir(outside)


async def test_an_abort_waits_for_a_scratch_worker_before_closing_the_root(tmp_path, monkeypatch):
    # A worker thread outlives a cancelled await: the root's descriptor closes only after the worker is done with it.
    scratch, outside = scratch_case(tmp_path)
    entered, release, seen = threading.Event(), threading.Event(), []
    original = ScratchRoot.write

    def blocked(self, name, arguments):
        entered.set()
        release.wait(5)
        try:
            os.fstat(self._fd)
            seen.append("open")
        except OSError:
            seen.append("closed")
        return original(self, name, arguments)

    monkeypatch.setattr(ScratchRoot, "write", blocked)
    write = ToolCallBlock("w", "write", {"path": str(scratch / "plan.md"), "content": "plan"})
    model = Model(after_checkpoint(), [[Done(AssistantMessage((write,), assistant().origin, "tool_use"))]])
    context = ContextConfig(scratch_dir=str(scratch))
    agent = make_agent(model, tools=[reader(tokens(3_000))], context=context, selection=ROOMY)
    await run(agent)
    compacting = asyncio.ensure_future(_drain(agent.run(input_row("next", tokens(23_000)), turn_id="next")))
    await asyncio.to_thread(entered.wait, 5)
    agent.abort("stop")
    threading.Timer(0.2, release.set).start()
    events = await compacting
    assert events[-1].reason == "aborted"
    assert seen == ["open"]
    assert (scratch / "plan.md").read_text() == "plan"
    assert sorted(os.listdir(scratch)) == ["escape", "plan.md"] and not os.listdir(outside)  # no temp file left


async def _drain(stream):
    return [event async for event in stream]


@pytest.mark.parametrize("swap", ["root", "target"])
async def test_a_scratch_write_never_lands_outside_after_a_swap_between_policy_and_execution(tmp_path, monkeypatch, swap):
    scratch, outside = flat_scratch(tmp_path)
    decide = CheckpointPolicy.decide

    def decide_then_swap(policy, call):
        decision = decide(policy, call)
        if swap == "root":
            # The root renamed and its pathname pointed at an outside directory, after the policy opened it.
            scratch.rename(scratch.parent / "moved")
            scratch.symlink_to(outside, target_is_directory=True)
        else:
            (scratch / "plan.md").symlink_to(outside / "plan.md")
        return decision

    monkeypatch.setattr(CheckpointPolicy, "decide", decide_then_swap)
    calls = [ToolCallBlock("w", "write", {"path": "state/scratch/session/plan.md", "content": "plan"})]
    results = await _scratch_checkpoint(tmp_path, scratch, calls, [WriteTool()])
    assert not os.listdir(outside)  # nothing outside: no content, no temp file, no directory
    if swap == "root":
        # The opened directory still receives it, under its new name; its old pathname is never resolved.
        assert not results["w"].is_error and (scratch.parent / "moved" / "plan.md").read_text() == "plan"
    else:
        # A name that became a symlink is never followed and never replaced.
        assert results["w"].content[0].text.endswith("it is not a regular file.")
        assert (scratch / "plan.md").is_symlink()


async def test_scratch_edit_reads_and_publishes_through_the_root_descriptor(tmp_path):
    scratch, outside = flat_scratch(tmp_path)
    scratch.mkdir(parents=True)
    (scratch / "plan.md").write_bytes("\ufeffa\r\nb\r\n".encode())
    (scratch / "link.md").symlink_to(outside / "secret.md")
    (outside / "secret.md").write_text("a\n")
    calls = [
        ToolCallBlock("e", "edit", {"path": "state/scratch/session/plan.md", "edits": [{"oldText": "b", "newText": "c"}]}),
        ToolCallBlock("l", "edit", {"path": "state/scratch/session/link.md", "edits": [{"oldText": "a", "newText": "x"}]}),
        ToolCallBlock("d", "write", {"path": "state/scratch/session/..", "content": "no"}),
    ]
    results = await _scratch_checkpoint(tmp_path, scratch, calls, [])
    assert results["e"].content[0].text == "Successfully replaced 1 block(s) in state/scratch/session/plan.md."
    assert (scratch / "plan.md").read_bytes() == "\ufeffa\r\nc\r\n".encode()  # BOM and CRLF kept
    assert results["l"].is_error and (outside / "secret.md").read_text() == "a\n"  # a symlink is never followed
    assert results["d"].content[0].text == DENIED


async def test_without_descriptor_relative_rename_scratch_writes_are_denied_and_reads_stay(tmp_path, monkeypatch):
    scratch, outside = flat_scratch(tmp_path)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset(os.supports_dir_fd) - {os.rename})  # as on Windows
    calls = [
        ToolCallBlock("w", "write", {"path": "state/scratch/session/plan.md", "content": "no"}),
        ToolCallBlock("r", "read", {"path": "notes.md"}),
    ]
    results = await _scratch_checkpoint(tmp_path, scratch, calls, [])
    assert results["w"].content[0].text == DENIED and not (scratch / "plan.md").exists()
    assert not results["r"].is_error
