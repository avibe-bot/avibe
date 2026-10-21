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

from core.agent_core.agent.fork import DREAMING, Decision, PolicyGate
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
from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.openai_chat import build_chat_payload
from core.agent_core.ai.openai_responses import build_responses_payload
from core.agent_core.ai.provider import Done, ModelEndpoint, ProviderError, TextDelta
from core.agent_core.agent.models import ModelSelection
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.context import (
    MEMO_FILE,
    MEMO_HEADING,
    MIND_CUT,
    MIND_FILE,
    MIND_CURRENT,
    MEMO_FILE_EMPTY,
    MIND_FILE_FIRST,
    MIND_HEADINGS,
    MIND_PLACEHOLDERS,
    MIND_SECTIONS,
    MIND_FILE_PREVIOUS,
    MIND_TEMPLATE,
    SKILLS_LEAD_CARRIED,
    text_tokens,
    MemoItem,
    memo_tokens,
    review_mind,
    memo_budget,
    mind_limits,
    mind_revert,
    CHECKPOINT_FRAMING,
    CHECKPOINT_REQUEST,
    CHECKPOINT_TRUNCATED,
    CACHE_TTL_UNKNOWN_S,
    TOOL_TRUNCATED,
    ContextConfig,
    StateRequest,
    checkpoint_repair,
    compaction_payload,
    message_tokens,
    request_tokens,
)
from core.agent_core.harness.projection import context_view, project
from core.agent_core.messages import (
    AssistantMessage,
    HostedAction,
    HostedCost,
    HostedNative,
    HostedRecord,
    HostedSummary,
    HostedToolBlock,
    Origin,
    TextBlock,
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
    ORIGIN,
    WINDOW_REFUSAL,
    anthropic_error,
    assistant,
    input_row,
    model_hub_error,
)

DENIED, BUDGET_USED = DREAMING.denied, DREAMING.budget_used

#: The engine tests' route: a 64,000-token window read through a 32,000-token input limit, so M = 8,000 and
#: T = 19,904 (section 1), the numbers every test here is sized against.
SELECTION = ModelSelection(ENDPOINT, replace(FAKE_SELECTION.capabilities, context_window=64_000, input_limit=32_000))
THRESHOLD = 19_904
#: A checkpoint as the template lays it out (section 11), above every floor (section 6): 128 tokens and more.
CHECKPOINT_BODY = (
    "# 1. Self and method\n- Report results tersely; ask before deleting anything.\n"
    "# 2. Goals and requirements\n- Goal: fix the failing parser test in src/parse.py without changing its public API.\n"
    "- Every change needs a test; keep the existing fixtures.\n"
    "# 3. Now and next\n- Done: reproduced the failure with `pytest -k parse` (KeyError: 'quote').\n"
    "- Next: patch the tokenizer's quote handling in src/parse.py, then rerun the whole parser suite and report.\n"
    "## Exact references\n- src/parse.py, tests/test_parse.py, `pytest -k parse`"
)


def block(body: str) -> str:
    """A final reply holding ``body`` as its one checkpoint block (section 6)."""
    return f"<checkpoint>\n{body}\n</checkpoint>"


CHECKPOINT = block(CHECKPOINT_BODY)


def tokens(count: int) -> str:
    return "x" * (4 * count)


def is_checkpoint(request) -> bool:
    return any(
        isinstance(message, UserMessage)
        and message.content[0].text is not None
        and message.content[0].text.startswith("<context-checkpoint-request>")
        for message in request.messages
    )


def checkpoint_message(request) -> str:
    """The checkpoint message's text: first in the request, or right after a split turn's kept input (section 5)."""
    texts = [message.content[0].text or "" for message in request.messages[:2] if isinstance(message, UserMessage)]
    return next(text for text in texts if text.startswith("<context-checkpoint>"))


def bulky(count: int) -> str:
    """A final reply whose checkpoint is about ``count`` tokens long."""
    return block(f"# 1. Self and method\n- {tokens(count)}\n# 2. Goals and requirements\n- the goal\n# 3. Now and next\n- next")


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
    """The engine tests' host: every input is a person's message, typed as its row says, unless ``typed`` names the
    ones that are; ``skills`` are the skills it can render, by name."""

    def __init__(self, typed=None, skills=None) -> None:
        self.states: list[StateRequest] = []
        self.records: list[tuple[str, int]] = []
        self.typed = typed
        self.skills = dict(skills or {})
        self.asked: list[list[str]] = []
        self.rendered: list[list[str]] = []

    def user_messages(self, row_ids):
        self.asked.append(list(row_ids))
        if self.typed is None:
            return {row_id: f"typed {row_id}" for row_id in row_ids}
        return {row_id: self.typed[row_id] for row_id in row_ids if row_id in self.typed}

    def skill_contents(self, session_id, names):
        self.rendered.append(list(names))
        return {name: (f"rev-{name}", self.skills[name]) for name in names if name in self.skills}

    def earlier_record(self, session_id, through_seq):
        self.records.append((session_id, through_seq))
        return f"lookup {session_id} through {through_seq}"

    async def render_state(self, request):
        self.states.append(request)
        return ["<state/>"]


def make_agent(
    model, *, store=None, tools=(), hooks=(), selection=SELECTION, context=None, cwd="/test-owned", **options
):
    selections = selection if isinstance(selection, tuple) else (selection,)
    return Agent(
        session_id="session",
        models=FakeModelRouter(model, selections),
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


async def at_threshold(store, selection, threshold: int, sizes, **options) -> int:
    """A first run on ``selection`` of reads sized ``sizes`` that stays under ``threshold``; the tokens of an input
    that brings the next request to it exactly. No reply reports usage, so nothing anchors and the estimate is the
    request's ``tokens()`` (section 2)."""
    model = Model(reads(len(sizes), final="noted"))
    agent = make_agent(model, store=store, tools=[sized_reader(*sizes)], selection=selection, **options)
    events = await run(agent, row="history")
    assert events[-1].reason == "completed" and not model.checkpoint_requests
    last = model.requests[-1]
    return threshold - request_tokens(last.system, last.tools, project(await store.load("session")).messages)


async def test_a_threshold_checkpoint_forks_the_exact_prefix_and_leaves_checkpoint_and_tail():
    host = Host()
    model = Model(growing(4), [[TextDelta(0, "streamed checkpoint"), Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))], context=ContextConfig(host=host))
    events = await run(agent)

    previous, fork, after = model.requests[3], model.requests[4], model.requests[5]
    assert [is_checkpoint(request) for request in model.requests] == [False] * 4 + [True, False]
    # Cache safety: the fork is the previous request extended, with every cache-relevant setting unchanged.
    assert fork.messages[: len(previous.messages)] == previous.messages
    assert fork.messages[-1].content[0].text.startswith(CHECKPOINT_REQUEST.split("{scratch}")[0])
    for field in ("endpoint", "system", "tools", "reasoning_effort", "supports_images", "cache", "max_tokens"):
        assert getattr(fork, field) == getattr(previous, field)

    rows = await agent.store.load("session")
    compaction = next(row for row in rows if row.kind == "compaction")
    before = [row for row in rows if row.context_seq < compaction.context_seq]
    assert fork.messages[:-1] == project(before).messages  # the whole conversation, then the request
    assert compaction.payload["mode"] == "normal" and compaction.payload["reason"] == "threshold"
    assert compaction.payload["checkpoint"] == CHECKPOINT_BODY
    assert host.states == [StateRequest("session")]
    assert host.records == [("session", compaction.payload["summarized_to_seq"])]

    # The next request: the split turn's input as it was, the checkpoint message (summary, then state), then the
    # kept tool batch.
    pinned, checkpoint, *tail = after.messages
    summary, state = (block.text for block in checkpoint.content)
    assert CHECKPOINT_BODY in summary and "<current-request>" not in summary
    assert pinned == next(row.message for row in rows if row.kind == "input")
    assert "lookup session through" in summary and state == "<state/>"
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
    assert (audit["outcome"], audit["folded_event_id"]) == ("completed", compaction.row_id)
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
@pytest.mark.parametrize(
    "limits,read,asked",
    [
        ((64_000, 32_000, 4_096), 6_000, 4_096),  # O up to 64,000: the conversation's own output cap
        ((400_000, 272_000, 128_000), 33_000, 64_000),  # T = 132,000; above 64,000 a checkpoint asks for 64,000
    ],
    ids=["O 4,096", "O 128,000"],
)
async def test_the_fork_is_a_byte_identical_prefix_on_every_protocol_wire(protocol, limits, read, asked):
    window, input_limit, output = limits
    endpoint = ModelEndpoint(protocol, "http://model.invalid", "test-model", "", provider="test-provider")
    capabilities = replace(
        SELECTION.capabilities,
        context_window=window,
        input_limit=input_limit,
        max_output_tokens=output,
        supports_reasoning=True,
        reasoning_efforts=("high",),
    )
    selection = ModelSelection(endpoint, capabilities)
    model = Model(growing(4), [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(read))], selection=selection, max_tokens=output)
    agent.reasoning_effort = "high"
    await run(agent)
    previous, fork = model.requests[3], model.requests[4]
    assert is_checkpoint(fork) and fork.reasoning_effort == "high"
    assert (previous.max_tokens, fork.max_tokens) == (output, asked)
    previous_settings, previous_items = await _wire(previous)
    fork_settings, fork_items = await _wire(fork)
    # Model, system, tools, tool choice, thinking, and effort are the conversation's; only the output cap may differ.
    assert fork_settings == (await _wire(replace(previous, max_tokens=asked)))[0]
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

    policy = PolicyGate(DREAMING, cwd=str(tmp_path), scratch_dir=str(tmp_path / "scratch"))
    monkeypatch.setattr("core.agent_core.agent.fork.os.path.commonpath", other_drive)
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
        (ThinkingBlock(tokens(5_800)), ToolCallBlock("peek", "read", {"path": "f"})), assistant().origin, "tool_use"
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


#: A 128,000-token route answering up to 32,000 tokens: M = 8,000 and T = 88,000 (section 1).
WIDE = ModelSelection(
    ENDPOINT, replace(SELECTION.capabilities, context_window=128_000, input_limit=None, max_output_tokens=32_000)
)


@pytest.mark.parametrize(
    "room,ran,last",
    [(4_000, ["peek"], CHECKPOINT_TRUNCATED.format(shown=3_000, total=10_000)), (3_999, [], BUDGET_USED)],
    ids=["room 4,000", "room 3,999"],
)
async def test_a_checkpoint_turn_at_T_reads_only_while_the_window_leaves_the_floor_after_its_output(room, ran, last):
    # At est = T on WIDE a checkpoint asks for all of O, 32,000, so the room its tools have,
    # L_in - est - min(64,000, O) - reserved, is about M less the checkpoint request and the response (section 6).
    # The response is sized to leave exactly ``room``: a read runs from 4,000 tokens on, its result cut to
    # room - 1,000, and one token less denies it.
    store = InMemoryTranscriptStore()
    size = await at_threshold(store, WIDE, 88_000, [20_000] * 4, max_tokens=32_000)

    def peek(request):
        read = ToolCallBlock("peek", "read", {"path": "f"})  # 4 tokens
        pad = 128_000 - request_tokens(request.system, request.tools, request.messages) - 32_000 - room - 4
        return [Done(AssistantMessage((text(tokens(pad)), read), assistant().origin, "tool_use"))]

    tool = reader(tokens(10_000))
    model = Model([[Done(assistant("done"))]], [peek, [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, store=store, tools=[tool], selection=WIDE, max_tokens=32_000)
    events = await run(agent, tokens(size), row="crossing")
    first, second = model.checkpoint_requests
    assert request_tokens(first.system, first.tools, first.messages[:-1]) == 88_000  # est = T
    assert (first.max_tokens, second.max_tokens) == (32_000, 32_000)
    assert [ctx.tool_call_id for _, ctx in tool.calls] == ran
    result = second.messages[-1]
    assert result.content[-1].text == last and message_tokens(result) <= room - 1_000
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["normal"]
    assert events[-1].reason == "completed"


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


#: A request the provider refuses because its input plus ``max_tokens`` exceeds the window (an Anthropic model before
#: Claude 4.5), at the endpoint and through Model Hub, classified as the Anthropic adapter classifies an HTTP error.
WINDOW_REFUSALS = [
    pytest.param(classify_error(status=400, body=json.dumps(envelope(WINDOW_REFUSAL)), protocol="anthropic"), id=name)
    for name, envelope in (("window-endpoint", anthropic_error), ("window-model-hub", model_hub_error))
]


@pytest.mark.parametrize("refusal", WINDOW_REFUSALS)
async def test_a_threshold_checkpoint_the_provider_refuses_for_its_window_rolls(refusal):
    # The threshold checkpoint's request can fit by the estimate, but the provider refuses it for its window: an
    # overflow, so the ladder goes on at (b) and rolls (section 3, section 8 a).
    store = InMemoryTranscriptStore()
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY))
    model = Model([[Done(assistant("after"))]], [[refusal], [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY)
    events = await cross(agent)
    finished = [(event.reason, event.mode) for event in events if isinstance(event, CompactionFinished)]
    assert finished == [("overflow", "rolling")]
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert [event.reason for event in failed] == ["threshold"] and failed[0].error.startswith("overflow: ")
    normal, rolling = model.checkpoint_requests
    assert len(rolling.messages) < len(normal.messages)  # the rolling fork carries a prefix of the context
    assert events[-1].reason == "completed"


#: Checkpoint turns that fail without a repair: a length stop, and a provider error.
FAILURES = [
    [Done(assistant(CHECKPOINT, stop_reason="length"))],
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


async def test_a_hosted_call_a_checkpoint_turn_makes_is_recorded_with_its_cost_and_the_checkpoint_holds():
    """MH-HOSTED-VIBEY-002, C-10 F2: a side turn keeps the native hosted declarations, so a provider may run a hosted
    call during a checkpoint. That call cannot be denied at execution; it is parsed like any other, kept in the turn's
    audit with its reported cost, and the checkpoint text beside it still holds."""
    search = HostedToolBlock(
        id="srvtoolu_1",
        tool="web_search",
        action=HostedAction("search", queries=("release notes",)),
        record=HostedRecord(status="completed"),
        native=HostedNative(source_id="src_served00001", payload=({"type": "server_tool_use", "id": "srvtoolu_1"},)),
    )
    summary = HostedSummary(counts={"web_search_requests": 1}, cost=HostedCost(basis="reported", usd=0.01))
    reply = AssistantMessage((search, text(CHECKPOINT)), ORIGIN, "stop", hosted_summary=summary)
    agent = make_agent(Model(after_checkpoint(), [[Done(reply)]]), tools=[reader(tokens(3_000))], selection=ROOMY)
    await run(agent)
    assert (await cross(agent))[-1].reason == "completed"
    assert [row for row in await agent.store.load("session") if row.kind == "compaction"]
    audited = agent.store.audits[0][2]["messages"][-1]
    assert audited["content"][0]["type"] == "hosted_tool"
    assert audited["hosted_summary"]["cost"] == {"basis": "reported", "usd": 0.01}


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
        "server",
    ]
    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed"] * 2
    assert events[-1].reason == "completed"
    # Nothing is kept: the next run tries again.
    model.conversation.extend([call("read", "again", path="f"), [Done(assistant("ok"))]])
    model.checkpoint.append([Done(assistant(CHECKPOINT))])
    events = await run(agent, row="next")
    assert len(model.checkpoint_requests) == 3 and events[-1].reason == "completed"
    assert [row.kind for row in await store.load("session")].count("compaction") == 1


#: A checkpoint turn the provider's safeguards refuse, saying why (the adapter's ``error_message``).
REFUSED = [Done(replace(assistant("", stop_reason="refusal"), error_message="category: reasoning_extraction"))]


@pytest.mark.parametrize(
    "sizes",
    [
        # Requests 5 and 6 cross T and their checkpoints are refused, which uses the bound up; the request a
        # 12,000-token result leaves cannot fit.
        pytest.param((6_000, 6_000, 6_000, 2_000, 500, 12_000), id="bound-used-earlier"),
        # Request 5's checkpoint is refused (one); request 6 cannot fit, and its own checkpoint is refused (two).
        pytest.param((6_000, 6_000, 6_000, 2_000, 9_000), id="bound-used-by-this-request"),
    ],
)
async def test_a_run_that_takes_no_more_checkpoints_drops_a_request_that_cannot_fit_and_continues(sizes):
    model = Model(reads(len(sizes)), [REFUSED, REFUSED])
    agent = make_agent(model, tools=[sized_reader(*sizes)])
    events = await run(agent)
    assert len(model.checkpoint_requests) == 2
    assert [event.error for event in events if isinstance(event, CompactionFailed)] == [
        "The checkpoint turn stopped with refusal (category: reasoning_extraction) and no usable checkpoint."
    ] * 2
    # The drop calls no model: the earliest part moves out, and the run goes on.
    drops = [row.payload for row in await agent.store.load("session") if row.kind == "compaction"]
    assert drops and all((drop["mode"], drop["reason"], drop["summarizer"]) == ("dropped", "overflow", None) for drop in drops)
    assert events[-1].reason == "completed" and not [e for e in events if isinstance(e, ContextExhausted)]


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
    big = [[Done(assistant(bulky(16_000)))], [Done(assistant(CHECKPOINT))]]
    model = Model(reads(6), big)
    agent = make_agent(model, tools=[sized_reader(5_000, 5_000, 5_000, 2_000, 2_000, 2_000)])
    events = await run(agent)
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["normal", "rolling"]
    for request in model.conversation_requests:
        assert request_tokens(request.system, request.tools, request.messages) + 4_096 + 8_000 <= 32_000


async def test_two_ineffective_checkpoints_stop_compaction_for_the_run():
    bloated = [[Done(assistant(bulky(13_000)))] for _ in range(2)]
    model = Model(reads(7), bloated)
    agent = make_agent(model, tools=[sized_reader(6_000, 6_000, 6_000, 6_000, 5_000, 5_000, 5_000)])
    events = await run(agent)
    finished = [event for event in events if isinstance(event, CompactionFinished)]
    normal = [event for event in finished if event.mode == "normal"]
    assert len(normal) == 2 and all(event.tokens_after_estimate >= 0.75 * THRESHOLD for event in normal)
    assert len(model.checkpoint_requests) == 2  # the next crossing is not tried in this run
    # The request that no longer fits is dropped instead, with no model call, and the run completes.
    assert [(event.reason, event.mode) for event in finished[2:]] == [("overflow", "dropped")]
    assert events[-1].reason == "completed"


OVERFLOW = ProviderError("overflow", "prompt is too long: 40000 tokens > 32000 maximum", False, status=400)


@pytest.mark.parametrize("refusal", [pytest.param(OVERFLOW, id="prompt-too-long"), *WINDOW_REFUSALS])
async def test_a_provider_overflow_below_T_takes_a_normal_checkpoint_and_retries(refusal):
    model = Model(
        [call("read", "r0", path="f"), call("read", "r1", path="f"), [refusal], [Done(assistant("done"))]],
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
    model = Model([[Done(assistant("done"))]], [[Done(assistant(block(f"{CHECKPOINT_BODY} ROLL ONE")))], [Done(assistant(block(f"{CHECKPOINT_BODY} ROLL TWO")))]])
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
    # Rolling again: the second fork's prefix leads with the first checkpoint.
    assert "ROLL ONE" in checkpoint_message(second)
    assert "ROLL TWO" in checkpoint_message(request)
    assert rows[-1].kind == "response" and request.messages == project(rows[:-1]).messages
    assert request_tokens(request.system, request.tools, request.messages) + 4_096 + 8_000 <= 32_000
    assert events[-1].reason == "completed"


#: A 1,000,000-token route answering up to 128,000 tokens: M = 30,000 and T = 842,000 (section 1).
MILLION = ModelSelection(
    ENDPOINT, replace(SELECTION.capabilities, context_window=1_000_000, input_limit=None, max_output_tokens=128_000)
)


def _calls(request) -> list[str]:
    return [call.id for message in request.messages if isinstance(message, AssistantMessage) for call in message.tool_calls]


async def test_a_threshold_checkpoint_on_a_1m_route_asks_for_64k_and_can_fit_at_T():
    store = InMemoryTranscriptStore()
    size = await at_threshold(store, MILLION, 842_000, [100_000] * 8, max_tokens=128_000)
    model = Model([[Done(assistant("done"))]], [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, store=store, tools=[reader("")], selection=MILLION, max_tokens=128_000)
    events = await run(agent, tokens(size), row="crossing")
    (fork,) = model.checkpoint_requests
    # The request is at T exactly. Its fork, the whole context and the checkpoint request, asks for 64,000 and can
    # fit: T reserves O (section 1).
    assert request_tokens(fork.system, fork.tools, fork.messages[:-1]) == 842_000
    assert fork.max_tokens == 64_000
    assert request_tokens(fork.system, fork.tools, fork.messages) + 64_000 <= 1_000_000
    finished = [(event.reason, event.mode) for event in events if isinstance(event, CompactionFinished)]
    assert finished == [("threshold", "normal")]
    assert [request.max_tokens for request in model.conversation_requests] == [128_000]  # the conversation asks for O
    assert events[-1].reason == "completed"


async def test_the_overflow_ladder_on_a_1m_route_asks_for_64k_and_still_rolls_at_half_the_tokens():
    # The provider refuses a request under T (it counts more than the estimate), then the normal checkpoint over the
    # whole context too, so the ladder rolls (section 8 a, b). Each fork asks for 64,000 and can fit, and the rolling
    # one cuts nearest half the tokens, as with 16,000: the first four of the eight reads move out.
    model = Model(
        [*_long_context(8), [OVERFLOW], [Done(assistant("done"))]], [[OVERFLOW], [Done(assistant(CHECKPOINT))]]
    )
    agent = make_agent(model, tools=[reader(tokens(100_000))], selection=MILLION, max_tokens=128_000)
    events = await run(agent)
    assert [event.mode for event in events if isinstance(event, CompactionFinished)] == ["rolling"]
    normal, rolling = model.checkpoint_requests
    refused, sent = model.conversation_requests[-2:]
    assert normal.messages[:-1] == refused.messages and _calls(rolling) == ["r0", "r1", "r2", "r3"]
    for fork in (normal, rolling):
        assert fork.max_tokens == 64_000
        assert request_tokens(fork.system, fork.tools, fork.messages) + 64_000 <= 1_000_000
    assert _calls(sent) == ["r4", "r5", "r6", "r7"]
    assert {request.max_tokens for request in model.conversation_requests} == {128_000}
    assert events[-1].reason == "completed"


#: The reply of the E2E checkpoint turn that replaced a Session's history: a write the turn denied, then a meta note.
_E2E_WRITE = ToolCallBlock(
    "toolu_write",
    "write",
    {"path": "/home/avibe/.avibe/workdir/e2e-scratch/CHECKPOINT.md", "content": "# 1. Self and method\n- strict"},
)
_E2E_NOTE = (
    "<silent>The function call was rejected because this is a checkpoint turn. I have attempted to write the "
    "checkpoint but hit the tool budget. The checkpoint content was prepared and ready; it documents the seven "
    "codewords collected (f01\u2013f07).</silent>"
)


NO_BLOCK = "it has no <checkpoint> block"


async def test_a_checkpoint_turn_that_replies_with_a_meta_note_fails_after_its_one_repair_and_keeps_the_context():
    # The E2E reply: the model tried to write the checkpoint to a file, was denied, then replied with a <silent> note.
    # That is no checkpoint; the one repair names the problem, and a second note fails the turn: it counts, keeps the
    # old context, and both replies' text lives only in the audit row.
    model = Model(
        growing(4),
        [
            [Done(AssistantMessage((_E2E_WRITE,), assistant().origin, "tool_use"))],
            [Done(assistant(_E2E_NOTE))],
            [Done(assistant(_E2E_NOTE))],
        ],
    )
    agent = make_agent(model, tools=[reader(tokens(6_000))])
    events = await run(agent)
    failed = [event for event in events if isinstance(event, CompactionFailed)]
    assert [event.error for event in failed] == [f"The checkpoint turn's reply is not a checkpoint: {NO_BLOCK}."]
    assert events[-1].reason == "completed" and agent._unproductive == 1
    rows = await agent.store.load("session")
    assert not [row for row in rows if row.kind == "compaction"]
    sent = model.conversation_requests[-1]
    assert sent.messages == project(rows[: len(rows) - 1]).messages  # the old context, whole
    assert not any("<silent>" in (block.text or "") for message in sent.messages for block in message.content
                   if isinstance(block, TextBlock))
    (audit,) = [payload for _, _, payload in agent.store.audits]  # the checkpoint-turn audits
    assert audit["outcome"] == "failed" and audit["repair"] == NO_BLOCK
    assert json.dumps(audit["messages"], ensure_ascii=False).count(_E2E_NOTE) == 2
    # The denial told the model where its files and its now belong; the repair named what was missing.
    denied = model.checkpoint_requests[1].messages[-1]
    assert "Write your now as your reply text" in denied.content[0].text
    assert model.checkpoint_requests[2].messages[-1] == checkpoint_repair(NO_BLOCK)


async def test_one_repair_turns_a_reply_without_a_block_into_the_checkpoint():
    model = Model(growing(4), [[Done(assistant(CHECKPOINT_BODY))], [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))])
    events = await run(agent)
    assert not [event for event in events if isinstance(event, CompactionFailed)]
    first, repaired = model.checkpoint_requests
    # The follow-up request is the first one, then the rejected reply, then the repair naming its problem.
    assert repaired.messages[:-2] == first.messages
    assert repaired.messages[-2].content[0].text == CHECKPOINT_BODY
    assert repaired.messages[-1] == checkpoint_repair(NO_BLOCK)
    (compaction,) = [row for row in await agent.store.load("session") if row.kind == "compaction"]
    assert compaction.payload["checkpoint"] == CHECKPOINT_BODY


@pytest.mark.parametrize(
    "body",
    [
        "# 1. 自我与方法\n## 我在这里的工作方式\n- 一步只读一个文件，读完再汇报\n"
        "# 2. 目标与要求\n## 目标\n- 收集 f01 到 f27 的口令，不要跳过任何一个文件\n"
        "# 3. 现在与下一步\n## 进度\n- 已读 f01–f04，口令都已记下\n## 下一步\n1. 读 f05",
        "The user wants the codewords of f01 to f27, one file per step, reported after each read. I have read f01 to "
        "f04 and recorded their codewords; the next step is to read f05 and report its codeword before going on.",
        "### Goal\n- Collect the codewords of f01 to f27, one file per step.\n### State\n- f01 to f04 are read and "
        "recorded.\n### Next\n- Read f05 and report its codeword, then continue with f06.",
    ],
    ids=["translated-headings", "no-headings", "other-headings"],
)
async def test_a_checkpoint_block_with_any_headings_or_none_is_accepted_and_narration_around_it_is_dropped(body):
    model = Model(growing(4), [[Done(assistant(f"Here is the checkpoint.\n{block(body)}\nI stop here."))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))])
    await run(agent)
    (compaction,) = [row for row in await agent.store.load("session") if row.kind == "compaction"]
    assert compaction.payload["checkpoint"] == body
    assert "repair" not in agent.store.audits[0][2]  # accepted as written, with no follow-up


async def test_a_reply_that_continues_the_task_under_numbered_headings_is_no_checkpoint():
    # The heading validator accepted this: three numbered sections, and the history would have been replaced by it.
    continuation = (
        "## 1. Reading the next file\nI'll read f05 now.\n## 2. The plan\nThen f06 and f07.\n"
        "## 3. Next\nI will report each codeword as I go."
    )
    model = Model(growing(4), [[Done(assistant(continuation))], [Done(assistant(continuation))]])
    agent = make_agent(model, tools=[reader(tokens(6_000))])
    events = await run(agent)
    assert [event.error for event in events if isinstance(event, CompactionFailed)] == [
        f"The checkpoint turn's reply is not a checkpoint: {NO_BLOCK}."
    ]
    assert len(model.checkpoint_requests) == 2
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]


async def test_only_the_final_response_counts_a_block_written_in_a_tool_calling_response_or_in_thinking_is_never_used():
    origin = assistant().origin
    with_block = AssistantMessage((text(CHECKPOINT), ToolCallBlock("r", "read", {"path": "f"})), origin, "tool_use")
    thinking = AssistantMessage((ThinkingBlock(CHECKPOINT), text("Done.")), origin, "stop")
    model = Model(after_checkpoint(), [[Done(with_block)], [Done(thinking)], [Done(thinking)]])
    agent = make_agent(model, tools=[reader(tokens(3_000))], selection=ROOMY)
    await run(agent)
    events = await cross(agent)
    assert [event.error for event in events if isinstance(event, CompactionFailed)] == [
        f"The checkpoint turn's reply is not a checkpoint: {NO_BLOCK}."
    ]
    assert not [row for row in await agent.store.load("session") if row.kind == "compaction"]


async def test_a_turn_keeps_its_input_and_every_steer_verbatim_across_a_mid_turn_checkpoint():
    # The Turn's input and a steer it accepted mid-Turn both stay as they were, in order, ahead of the checkpoint: a
    # checkpoint that rewrote either ("read f10 to f18" as "continue with f19") would drift what the Turn answers.
    holder, steered = {}, []

    async def execute(arguments, ctx):
        if not steered:
            steered.append(await holder["agent"].steer(input_row("steer", "STEER: also report each size")))
        return ToolResult((text(tokens(6_000)),))

    model = Model(growing(4), [[Done(assistant(CHECKPOINT))]])
    holder["agent"] = agent = make_agent(model, tools=[FakeTool("read", execute=execute)])
    events = await run(agent, "read f0 to f3")
    assert steered == [True] and events[-1].reason == "completed" and len(model.checkpoint_requests) == 1
    first, steer, checkpoint, *tail = model.conversation_requests[-1].messages
    assert [first.content[0].text, steer.content[0].text] == ["read f0 to f3", "STEER: also report each size"]
    assert checkpoint.content[0].text.startswith("<context-checkpoint>") and tail
    rows = await agent.store.load("session")
    (compaction,) = [row for row in rows if row.kind == "compaction"]
    inputs = [row.context_seq for row in rows if row.kind == "input"]
    assert compaction.payload["kept_inputs"] == inputs


async def test_a_small_fallback_drops_a_previous_checkpoint_too_large_for_it_and_continues():
    # A 15,000-token checkpoint written on a large route cannot fit an 8,000-token fallback: no fork can take it, and a
    # drop that kept it would still not fit. The drop leaves its model text out, says so, and the Turn continues.
    store = InMemoryTranscriptStore()
    await _history_on_a_large_window(store, [1_000])
    view = context_view(await store.load("session"))
    large = "# 1. Self and method\n" + tokens(15_000)
    payload = compaction_payload(
        view, len(view.units) - 1, mode="normal", reason="threshold", checkpoint=large, state=(),
        earlier_record="lookup", tokens_before=1, threshold=2, window=200_000, summarizer=None, usage=None,
    )
    await store.append_payloads("session", [("compaction", payload)])
    small = ModelSelection(
        ENDPOINT, replace(SELECTION.capabilities, context_window=8_000, input_limit=None, max_output_tokens=2_000)
    )
    model = Model([[Done(assistant("done"))]])
    agent = make_agent(model, store=store, selection=small, context=ContextConfig(host=Host()))
    events = await run(agent)
    assert events[-1].reason == "completed" and not model.checkpoint_requests
    dropped = [row for row in await store.load("session") if row.kind == "compaction"][-1]
    assert dropped.payload["mode"] == "dropped" and dropped.payload["checkpoint"] == ""
    summary = dropped.payload["summary"]
    assert "Self and method" not in summary
    assert "\nAn earlier checkpoint was too large for this model and was omitted; see the earlier record.\n" in summary
    assert "<earlier-record>" in summary


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
        assert dropped.payload["checkpoint"] == CHECKPOINT_BODY
        assert dropped.payload["summary"].startswith(f"<context-checkpoint>\n{CHECKPOINT_FRAMING}")
        assert dropped.payload["first_kept_seq"] > first_row.payload["first_kept_seq"]
    assert checkpoint_message(model.conversation_requests[-1]) == drops[-1].payload["summary"]
    assert events[-1].reason == "completed"


#: A 16,000-token route answering at most 4,000 tokens (the Avibe Agent's budget, a quarter of the window).
SMALL = ModelSelection(
    ENDPOINT, replace(SELECTION.capabilities, context_window=16_000, input_limit=None, max_output_tokens=4_000)
)


async def test_three_parallel_reads_larger_than_a_16k_window_are_cut_to_fit_and_the_turn_completes():
    # The E2E repro: a fresh session on a 16K route with a 4,000-token system prompt reads three 15.6 KB files in one
    # step. Nothing can move out, yet the run continues: the largest results are cut to one cap, each saying so.
    calls = [ToolCallBlock(f"r{index}", "read", {"path": f"ctx/part0{index}.txt"}) for index in range(3)]
    model = Model([[Done(assistant(calls=calls))], [Done(assistant("done"))]])
    agent = make_agent(model, tools=[reader("x" * 15_600)], selection=SMALL, system="s" * 16_000)
    events = await run(agent)
    assert events[-1].reason == "completed" and not [e for e in events if isinstance(e, ContextExhausted)]
    final = model.conversation_requests[-1]
    results = [message for message in final.messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 3 and all(result.content[0].text.endswith(TOOL_TRUNCATED) for result in results)
    assert len({message_tokens(result) for result in results}) == 1  # one cap for equal results
    # The request fits with the margin, so it is sent as it is.
    assert request_tokens(final.system, final.tools, final.messages) + 4_000 + 2_000 <= 16_000
    rows = await agent.store.load("session")
    # The rows keep every output whole; three edits cut what the model reads.
    assert [len(row.message.content[0].text) for row in rows if row.kind == "tool_result"] == [15_600] * 3
    assert [row.payload["reason"] for row in rows if row.kind == "context_edit"] == ["fit_tool_result"] * 3


async def test_a_tool_batch_the_ladder_can_make_room_for_is_never_cut():
    # The batch does not fit beside the history, but it fits once the history moves out: the ladder compacts, and
    # the result reaches the model whole.
    store = InMemoryTranscriptStore()
    await _history_on_a_large_window(store, [5_000, 5_000, 5_000, 2_000])
    model = Model([call("read", "big", path="f"), [Done(assistant("done"))]], [[Done(assistant(CHECKPOINT))]] * 3)
    agent = make_agent(model, store=store, tools=[reader(tokens(12_000))])
    events = await run(agent)
    assert events[-1].reason == "completed" and [e for e in events if isinstance(e, CompactionFinished)]
    rows = await store.load("session")
    assert not [row for row in rows if row.kind == "context_edit" and row.payload["reason"] == "fit_tool_result"]
    final = model.conversation_requests[-1]
    (result,) = [m for m in final.messages if isinstance(m, ToolResultMessage) and m.tool_call_id == "big"]
    assert result.content[0].text == tokens(12_000)


async def test_a_refused_batch_nothing_can_move_out_of_is_halved_until_the_provider_takes_it():
    # The provider counts more than the estimate: it refuses a request that fits here, and nothing older can move
    # out. The batch is the newest unit, so it is cut to half of what the model read, and the request is sent again.
    calls = [ToolCallBlock(f"r{index}", "read", {"path": f"f{index}"}) for index in range(3)]
    model = Model([[Done(assistant(calls=calls))], [OVERFLOW], [Done(assistant("done"))]])
    agent = make_agent(model, tools=[reader(tokens(2_000))], selection=SMALL)
    events = await run(agent)
    assert events[-1].reason == "completed" and not model.checkpoint_requests
    results = [m for m in model.conversation_requests[-1].messages if isinstance(m, ToolResultMessage)]
    assert all(result.content[0].text.endswith(TOOL_TRUNCATED) for result in results)
    assert sum(message_tokens(result) for result in results) <= 3 * 2_001 // 2


async def test_a_batch_the_provider_keeps_refusing_is_reported_only_after_it_was_shortened():
    # Refused on every request: the batch is halved after each refusal, and only then does the run say the tool
    # output does not fit even shortened, which is now true.
    calls = [ToolCallBlock(f"r{index}", "read", {"path": f"f{index}"}) for index in range(3)]
    model = Model([[Done(assistant(calls=calls))], *[[OVERFLOW]] * 4])
    agent = make_agent(model, tools=[reader(tokens(2_000))], selection=SMALL)
    events = await run(agent)
    assert events[-1].reason == "context_exhausted"
    assert [e.kind for e in events if isinstance(e, AgentError)] == ["tool_output_too_large"]
    sizes = [
        sum(message_tokens(m) for m in request.messages if isinstance(m, ToolResultMessage))
        for request in model.conversation_requests[1:]
    ]
    assert len(sizes) == 4 and all(later <= earlier // 2 + 3 for earlier, later in zip(sizes, sizes[1:]))


async def test_the_stop_names_the_step_when_its_own_arguments_are_what_does_not_fit():
    # A write whose arguments nearly fill the window and whose result is two words: the results are not the excess,
    # the step is, and the error says so instead of blaming the tool output.
    write = ToolCallBlock("w", "write", {"path": "big.txt", "content": tokens(13_000)})
    model = Model([[Done(assistant(calls=[write]))]])
    agent = make_agent(model, tools=[FakeTool("write", result=ToolResult((text("ok"),)))], selection=SMALL)
    events = await run(agent)
    assert events[-1].reason == "context_exhausted"
    assert [(e.kind, e.origin) for e in events if isinstance(e, AgentError)] == [("step_too_large", "local")]


async def test_a_refused_step_whose_results_are_already_at_their_notes_is_named_as_the_step():
    # The provider refuses, and the batch cannot be shortened (its result is smaller than a note): what is too large
    # is the step, not its output.
    model = Model([call("read", "r", path="f"), [OVERFLOW]])
    agent = make_agent(model, tools=[reader("tiny")], selection=SMALL)
    events = await run(agent)
    assert [e.kind for e in events if isinstance(e, AgentError)] == ["step_too_large"]


async def test_the_stop_names_the_tool_output_when_even_its_notes_cannot_fit():
    # 300 results on an 8,000-token route: cut to their notes, they still do not fit beside the step that asked for
    # them. The newest tool output is the cause, not the conversation's length, and the error says so.
    tiny = ModelSelection(
        ENDPOINT, replace(SELECTION.capabilities, context_window=8_000, input_limit=None, max_output_tokens=2_000)
    )
    calls = [ToolCallBlock(f"r{index}", "read", {"path": f"f{index}"}) for index in range(300)]
    model = Model([[Done(assistant(calls=calls))]])
    agent = make_agent(model, tools=[reader(tokens(100))], selection=tiny)
    events = await run(agent)
    assert events[-1].reason == "context_exhausted"
    assert [(e.kind, e.origin) for e in events if isinstance(e, AgentError)] == [("tool_output_too_large", "local")]
    exhausted = next(event for event in events if isinstance(event, ContextExhausted))
    assert {part.name for part in exhausted.parts} >= {"latest_tool_batch", "output"}
    assert not [row for row in await agent.store.load("session") if row.kind == "context_edit"]
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


async def test_the_turn_input_a_drop_would_keep_leaves_the_batch_only_the_room_after_it():
    # The last unit is a tool batch after a large input: moving everything else out still keeps that input, the
    # Turn's, so the batch gets only the room left beside it. It is cut to that, with no checkpoint call or row.
    model = Model([call("read", "big", path="f"), [Done(assistant("done"))]], [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(12_000))])
    events = await run(agent, tokens(18_000))
    assert events[-1].reason == "completed" and not model.checkpoint_requests
    final = model.conversation_requests[-1]
    (result,) = [message for message in final.messages if isinstance(message, ToolResultMessage)]
    assert result.content[0].text.endswith(TOOL_TRUNCATED) and message_tokens(result) < 2_000
    assert request_tokens(final.system, final.tools, final.messages) + 4_096 + 8_000 <= 32_000
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
    host = Host(typed={})  # no message a person typed: the drop carries nothing the minimal request leaves out
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
    # The reads, then the refused request: its normal and rolling checkpoints are refused too, which uses the run's
    # bound up, so each later refusal is answered by a drop; the fourth refusal of the request ends the run.
    assert len(model.conversation_requests) == 8 + 4
    assert events[-1].reason == "context_exhausted"
    drops = [row.payload for row in await agent.store.load("session") if row.kind == "compaction"]
    assert [(drop["mode"], drop["reason"]) for drop in drops] == [("dropped", "overflow")] * 3


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


#: An official OpenAI GPT model (a 30-minute prompt cache, section 3) and Anthropic, whose message breakpoints Vibey
#: caches for 5 minutes; ``ENDPOINT``'s test provider is a route of unknown lifetime, an hour.
GPT = ModelEndpoint("openai_responses", "http://model.invalid", "gpt-6-astra", "", provider="openai")
CLAUDE = ModelEndpoint("anthropic", "http://model.invalid", "claude-opus-5-5", "", provider="anthropic")


@pytest.mark.parametrize("endpoint,lifetime", [(CLAUDE, 300), (GPT, 1_800), (ENDPOINT, 3_600)])
async def test_clearing_runs_once_the_route_cache_has_gone_cold_and_spares_recent_turns(endpoint, lifetime):
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    context = ContextConfig(clock=lambda: now[0], clear_tool_results=True)
    script = [*_long_context(9), *([Done(assistant(word))] for word in ("one", "two", "three", "four"))]
    model = Model(script)
    route = ModelSelection(endpoint, LARGE.capabilities)
    agent = make_agent(model, store=store, tools=[reader(tokens(6_000))], selection=route, context=context)
    await run(agent, row="turn-1")
    await run(agent, row="turn-2")
    now[0] += lifetime  # idle exactly as long as the route's cache lives: still warm
    await run(agent, row="turn-3")
    assert not [row for row in await store.load("session") if row.kind == "context_edit"]  # warm, below 0.8 T
    results = deepcopy([row for row in await store.load("session") if row.kind == "tool_result"])
    now[0] += lifetime + 1  # idle past it
    await run(agent, row="turn-4")
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


async def test_old_tool_result_clearing_is_disabled_by_default():
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    model = Model([*_long_context(9), *([Done(assistant(word))] for word in ("one", "two", "three"))])
    agent = make_agent(
        model,
        store=store,
        tools=[reader(tokens(6_000))],
        selection=LARGE,
        context=ContextConfig(clock=lambda: now[0]),
    )
    await run(agent, row="turn-1")
    await run(agent, row="turn-2")
    now[0] += CACHE_TTL_UNKNOWN_S + 1
    await run(agent, row="turn-3")

    rows = await store.load("session")
    assert not [row for row in rows if row.kind == "context_edit"]
    assert not model.checkpoint_requests


#: A Model Hub Google hop as the adapter calls it: over Chat at the gateway, while the served-hop report names the
#: Google protocol the gateway converted to (C-6). Its cache lifetime is unknown: an hour.
GEMINI = ModelEndpoint("openai_chat", "http://model.invalid", "gemini-3-pro", "", provider="google")
GEMINI_SERVED = Origin("google", "google", "gemini-3-pro")


async def routed_turn(store, now, endpoint, scripts, row, served=None):
    """One Turn in a new Agent over ``store`` on ``endpoint``, its replies served as ``served`` (the endpoint's own
    origin by default); the ``context_edit`` rows after it."""
    origin = served or endpoint_origin(endpoint)
    model = Model([[replace(event, message=replace(event.message, origin=origin)) for event in s] for s in scripts])
    route = ModelSelection(endpoint, LARGE.capabilities)
    context = ContextConfig(clock=lambda: now[0], clear_tool_results=True)
    agent = make_agent(model, store=store, tools=[reader(tokens(6_000))], selection=route, context=context)
    assert (await run(agent, row=row))[-1].reason == "completed"
    return [row for row in await store.load("session") if row.kind == "context_edit"]


async def test_a_route_cache_counts_only_its_own_use():
    # Ten minutes after Claude's last reply, GPT answers for the first time: a route with no use yet takes the latest
    # use of any route, well inside GPT's 30 minutes, so nothing clears. A minute later the Session is back on Claude,
    # whose own cache went unused for eleven minutes: cold, although another route answered a minute ago.
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    await routed_turn(store, now, CLAUDE, [*_long_context(9), [Done(assistant("one"))]], "turn-1")
    await routed_turn(store, now, CLAUDE, [[Done(assistant("two"))]], "turn-2")
    now[0] += 600
    assert not await routed_turn(store, now, GPT, [[Done(assistant("three"))]], "turn-3")
    now[0] += 60
    assert len(await routed_turn(store, now, CLAUDE, [[Done(assistant("four"))]], "turn-4")) == 4


async def test_a_converted_route_cache_is_its_upstream_provider_and_model():
    # A Google hop's replies are served as the Google protocol though the request went over Chat: its cache is still
    # its own. Fifty minutes after its last reply it is warm (an hour, unknown lifetime); after another route answers,
    # it goes cold once its own reply is over an hour old, although that route answered under twelve minutes ago.
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    await routed_turn(store, now, GEMINI, [*_long_context(9), [Done(assistant("one"))]], "turn-1", GEMINI_SERVED)
    await routed_turn(store, now, GEMINI, [[Done(assistant("two"))]], "turn-2", GEMINI_SERVED)
    now[0] += 3_000
    assert not await routed_turn(store, now, GEMINI, [[Done(assistant("three"))]], "turn-3", GEMINI_SERVED)
    now[0] += 3_000
    assert not await routed_turn(store, now, ENDPOINT, [[Done(assistant("four"))]], "turn-4")
    now[0] += 700
    assert len(await routed_turn(store, now, GEMINI, [[Done(assistant("five"))]], "turn-5", GEMINI_SERVED)) == 4


async def test_pressure_clearing_runs_on_a_warm_cache():
    # From 0.8 T old results clear whatever the cache's lifetime (section 3): on this window T = 59,904, and nine
    # results of 6,000 tokens hold 0.9 T. The third Turn is the first whose old results are not protected.
    route = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=72_000, input_limit=None))
    script = [*_long_context(9), *([Done(assistant(word))] for word in ("one", "two", "three"))]
    model = Model(script, [[Done(assistant(CHECKPOINT))]])
    agent = make_agent(
        model,
        tools=[reader(tokens(6_000))],
        selection=route,
        context=ContextConfig(clear_tool_results=True),
    )
    for turn in ("turn-1", "turn-2", "turn-3"):
        assert (await run(agent, row=turn))[-1].reason == "completed"
    edits = [row for row in await agent.store.load("session") if row.kind == "context_edit"]
    assert [row.payload["reason"] for row in edits] == ["clear_old_tool_result"] * 4
    assert not model.checkpoint_requests


def responses_reply(*calls: ToolCallBlock, reasoning: int, value: str = ""):
    """A script answering on ``GPT`` with a Responses reasoning item of 17 bytes of ciphertext per reasoning token, billed
    as the OpenAI route was measured to bill: the request's bytes / 4 without the ciphertext, plus the reported
    reasoning of every item it replays."""

    def script(request):
        visible, replayed = 0, 0
        for message in request.messages:
            for block in message.content:
                if isinstance(block, (TextBlock, ThinkingBlock)):
                    visible += len(block.text.encode())
                elif isinstance(block, ToolCallBlock):
                    visible += len(block.name.encode()) + len(json.dumps(block.arguments, separators=(",", ":")))
            if isinstance(message, AssistantMessage) and any(
                isinstance(block, ThinkingBlock) and block.signature for block in message.content
            ):
                replayed += message.usage.reasoning_tokens
        billed = request_tokens(request.system, request.tools, ()) + -(-visible // 4) + replayed
        item = {"type": "reasoning", "id": "rs_0", "encrypted_content": "A" * (17 * reasoning), "summary": []}
        content = (ThinkingBlock("", signature=json.dumps(item)), *((text(value),) if value else ()), *calls)
        usage = Usage(input_tokens=billed, output_tokens=reasoning + 20, reasoning_tokens=reasoning)
        return [Done(AssistantMessage(content, endpoint_origin(GPT), "tool_use" if calls else "stop", usage=usage))]

    return script


@pytest.mark.parametrize("idle_s,cleared", [(1_302, 0), (1_860, 4)])
async def test_a_return_to_a_long_reasoning_session_never_checkpoints_at_57_percent(idle_s, cleared):
    # The incident: a gpt-6-astra Session about 57% of T into its window (here 102,000 of T = 180,000), its 36,400
    # reasoning tokens replayed as 155,000 tokens of ciphertext by UTF-8 / 4. After 21.7 minutes away its 30-minute
    # cache is still warm and nothing clears; after 31 minutes four old results clear, the anchor still holds, and
    # the estimate falls by what they held. Neither checkpoints.
    now = [1_000.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    reads = [responses_reply(ToolCallBlock(f"r{index}", "read", {"path": "f"}), reasoning=4_000) for index in range(9)]
    script = [*reads, *(responses_reply(value=word, reasoning=200) for word in ("noted", "two", "three"))]
    model = Model(script, [[Done(assistant(CHECKPOINT))]])
    route = ModelSelection(GPT, LARGE.capabilities)
    context = ContextConfig(clock=lambda: now[0], clear_tool_results=True)
    agent = make_agent(model, store=store, tools=[reader(tokens(7_300))], selection=route, context=context)
    await run(agent, row="turn-1")
    await run(agent, row="turn-2")
    billed = [row.message.usage.input_tokens for row in await store.load("session") if row.kind == "response"]
    assert 0.55 * 180_000 < billed[-1] < 0.6 * 180_000
    now[0] += idle_s
    events = await run(agent, row="turn-3")
    assert events[-1].reason == "completed" and not model.checkpoint_requests
    edits = [row for row in await store.load("session") if row.kind == "context_edit"]
    assert len(edits) == cleared and len(model.conversation_requests) == 12


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
        [[Done(assistant(block(f"{CHECKPOINT_BODY} {index}")))] for index in range(60)],
    )
    agent = make_agent(model, tools=[reader(tokens(2_500))])
    events = await run(agent)
    assert events[-1].reason == "completed"
    for request in model.conversation_requests:
        assert request_tokens(request.system, request.tools, request.messages) < THRESHOLD
    rows = await agent.store.load("session")
    compactions = [row for row in rows if row.kind == "compaction"]
    assert len(compactions) >= 3
    # Iterative: each later fork's prefix leads with the previous checkpoint, and file lists accumulate, the five
    # most recently read kept (section 7).
    assert all(checkpoint_message(request) for request in model.checkpoint_requests[1:])
    assert compactions[-1].payload["files_read"] == [f"f{index}" for index in range(6, 1, -1)]  # most recent first
    assert compactions[-1].payload["files_read_omitted"]
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
    model = Model(reads(4), [[Done(assistant(bulky(15_000)))]])
    agent = make_agent(model, store=store, tools=[sized_reader(6_000, 6_000, 6_000, 6_000)])
    events = await run(agent)
    rows = await store.load("session")
    # Nothing of the failed transition is anywhere: no checkpoint, no announcement.
    assert not [row for row in rows if row.kind == "compaction"]
    assert [type(event).__name__ for event in events if "Compaction" in type(event).__name__] == ["CompactionStarted"]
    assert events[-1].reason == "error"
    model = Model(reads(1), [[Done(assistant(bulky(15_000)))]])
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
    # The turn's own files beside the plan (section 6), and no temp file left.
    assert sorted(os.listdir(scratch)) == ["escape", "memo.md", "mind.md", "plan.md"] and not os.listdir(outside)


async def _drain(stream):
    return [event async for event in stream]


@pytest.mark.parametrize("swap", ["root", "target"])
async def test_a_scratch_write_never_lands_outside_after_a_swap_between_policy_and_execution(tmp_path, monkeypatch, swap):
    scratch, outside = flat_scratch(tmp_path)
    decide = PolicyGate.decide

    def decide_then_swap(policy, call):
        decision = decide(policy, call)
        if swap == "root":
            # The root renamed and its pathname pointed at an outside directory, after the policy opened it.
            scratch.rename(scratch.parent / "moved")
            scratch.symlink_to(outside, target_is_directory=True)
        else:
            (scratch / "plan.md").symlink_to(outside / "plan.md")
        return decision

    monkeypatch.setattr(PolicyGate, "decide", decide_then_swap)
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
    assert results["e"].content[0].text == "Successfully replaced 1 block(s) in state/scratch/session/plan.md (line 2)."
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


# --- checkpoint-v5: the mind, the memo, and the now (context.md sections 6, 7, and 10) ---------------------------


def _edit(call_id: str, path, *pairs) -> ToolCallBlock:
    return ToolCallBlock(call_id, "edit", {"path": str(path), "edits": [{"oldText": o, "newText": n} for o, n in pairs]})


def _batch(*calls) -> list:
    return [Done(AssistantMessage(tuple(calls), assistant().origin, "tool_use"))]


def _v5(tmp_path, host=None, **options) -> ContextConfig:
    return ContextConfig(
        host=host or Host(),
        scratch_dir=str(tmp_path / "scratch"),
        checkpoint_dir=str(tmp_path / "checkpoints"),
        **options,
    )


MISSION = "- fix the parser without changing its API (in progress)"
FIRST_MIND = MIND_TEMPLATE.replace(MIND_PLACEHOLDERS["## Mission"], MISSION)


def _fill_mind(tmp_path) -> list:
    """A checkpoint turn that writes the mission into the template, checks memo message #1, then replies."""
    scratch = tmp_path / "scratch"
    return [
        _batch(
            _edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Mission"], MISSION)),
            _edit("n", scratch / MEMO_FILE, ("- [ ] (#1)", "- [x] (#1)")),
        ),
        [Done(assistant(CHECKPOINT))],
    ]


async def _consolidate(tmp_path, scripts, *, host=None, store=None, crossings=1):
    """``history()``, then ``crossings`` Turns that each cross T on ``ROOMY`` and run the next checkpoint script."""
    store = store or InMemoryTranscriptStore()
    context = _v5(tmp_path, host)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    models = []
    for index, script in enumerate(scripts):
        model = Model([[Done(assistant("after"))]], script)
        agent = make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context)
        await cross(agent, row=f"cross{index}")
        models.append(model)
    rows = await store.load("session")
    return models, [row for row in rows if row.kind == "compaction"], store


def _request(model) -> str:
    return model.checkpoint_requests[0].messages[-1].content[0].text


async def test_a_checkpoint_consolidates_into_a_mind_it_edits_a_memo_it_checks_and_a_now(tmp_path):
    host = Host()
    (model,), (row,), store = await _consolidate(tmp_path, [_fill_mind(tmp_path)], host=host)
    # The turn starts with the template and the memo list in its scratch root, and the request says so (section 6).
    request = _request(model)
    assert MIND_FILE_FIRST in request
    # It names where the files are: a model told only "your scratch directory" went looking for them with bash.
    scratch = tmp_path / "scratch"
    assert f"Your scratch directory is {scratch}." in request and f"- {scratch}/mind.md: your mind." in request
    assert request.endswith("Current memo.md:\n- [ ] (#1) typed input\n</context-checkpoint-request>")
    # One question, off the event loop, about every input a cut could move out; the crossing Turn's own input is not
    # offered: it stays as it was.
    assert host.asked == [["input", "cross0"]]
    payload = row.payload
    assert (payload["mind"], payload["mind_truncated"]) == (FIRST_MIND, False)
    assert payload["memo"] == [{"seq": 1, "text": "typed input", "truncated": False}]
    assert payload["checkpoint"] == CHECKPOINT_BODY
    assert payload["summarizer"]["prompt_version"] == "checkpoint-v5" and payload["summarizer"]["rounds"] == 1
    summary = payload["summary"]
    assert summary.startswith(f"<context-checkpoint>\n{CHECKPOINT_FRAMING}\n\n{FIRST_MIND}\n\n{MEMO_HEADING}\n- (#1) typed input\n\n{CHECKPOINT_BODY}\n")
    (audit,) = [audit for _, _, audit in store.audits]
    assert audit["outcome"] == "completed" and "repair" not in audit
    edits = [message for message in audit["messages"] if message["role"] == "tool_result"]
    assert [message["is_error"] for message in edits] == [False, False]


async def test_with_no_memo_candidate_the_request_shows_no_list_and_the_turn_edits_only_the_mind(tmp_path):
    # A Session no person typed into since the last checkpoint (a Watch's, a Task's, another agent's inputs only): no
    # memo candidate. memo.md starts empty, so the request must not present any text as its content (a "(no messages)"
    # placeholder sent a real turn to edit the empty file, twice); it says the file is empty and to leave it so.
    scratch = tmp_path / "scratch"
    mind_only = [
        _batch(_edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Mission"], MISSION))),
        [Done(assistant(CHECKPOINT))],
    ]
    (model,), (row,), _ = await _consolidate(tmp_path, [mind_only], host=Host(typed={}))
    request = _request(model)
    assert (scratch / MEMO_FILE).read_text() == ""  # the file the turn found, and left
    assert "Current memo.md:" not in request  # no list: nothing shown that the file does not hold
    assert f"- {scratch}/memo.md: {MEMO_FILE_EMPTY}\n" in request
    assert len(model.checkpoint_requests) == 2  # the mind's edit, then the reply: no repair
    assert (row.payload["mind"], row.payload["memo"]) == (FIRST_MIND, [])
    assert "# 5. The user's words\n(none)" in row.payload["summary"]


async def test_a_first_consolidation_replaces_each_line_under_a_heading_alone_and_keeps_every_heading(tmp_path):
    # The first consolidation fills every section in one edit call whose oldTexts are the lines under the headings
    # alone, never a heading: each must be unique in the template, so the edit applies, every heading stays, and no
    # structure repair is needed. Under one "(none)" for all seven, the heading was the only unique oldText, and a
    # real turn's seven-part edit dropped one.
    scratch = tmp_path / "scratch"
    lines = MIND_TEMPLATE.split("\n")
    under = {
        line: lines[index + 1]
        for index, line in enumerate(lines[:-1])
        if line in MIND_HEADINGS and lines[index + 1] and lines[index + 1] not in MIND_HEADINGS
    }
    assert len(under) == 7
    contents = {heading: f"- {heading.lstrip('# ')}: filled" for heading in under}
    contents["## Mission"] = MISSION
    fill = [
        _batch(_edit("m", scratch / MIND_FILE, *((under[heading], body) for heading, body in contents.items()))),
        [Done(assistant(CHECKPOINT))],
    ]
    (model,), (row,), _ = await _consolidate(tmp_path, [fill])
    results = [m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)]
    assert not results[-1].is_error  # the one edit call applied
    assert len(model.checkpoint_requests) == 2  # the edit, then the reply: no repair
    mind = row.payload["mind"]
    assert review_mind(mind, MIND_TEMPLATE).structure is None
    assert all(heading in mind.split("\n") for heading in MIND_HEADINGS)
    assert all(f"{heading}\n{body}" in mind for heading, body in contents.items() if not heading.startswith("# "))


async def test_a_mind_stored_before_the_placeholders_is_filled_by_placeholder_edits(tmp_path):
    # A Session whose last checkpoint stored a mind under the earlier template, "(none)" in every empty section. Its
    # next checkpoint turn starts from that mind with the placeholders in place, in mind.md and in the request, so an
    # edit that names only a section's placeholder, as the request asks, fills it.
    store = InMemoryTranscriptStore()
    context = _v5(tmp_path, Host())
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    legacy = "\n\n".join(
        "\n".join([top, *(f"{sub}\n(none)" for sub in subs)] if subs else [top, "(none)"]) for top, _, subs in MIND_SECTIONS
    ).replace("## Mission\n(none)", f"## Mission\n{MISSION}")
    view = context_view(await store.load("session"))
    stored = compaction_payload(
        view, len(view.units) - 1, mode="normal", reason="threshold", checkpoint=CHECKPOINT_BODY, state=(),
        earlier_record=None, tokens_before=1, threshold=2, window=32_000, summarizer=None, usage=None, mind=legacy,
    )
    await store.append_payloads("session", [("compaction", stored)])
    scratch = tmp_path / "scratch"
    fill = [
        _batch(_edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Workstreams"], "- the parser fix: in progress"))),
        [Done(assistant(CHECKPOINT))],
    ]
    model = Model([[Done(assistant("after"))]], fill)
    await run(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context), tokens(29_000), row="cross")
    assert MIND_PLACEHOLDERS["## Workstreams"] in _request(model) and "\n(none)\n" not in _request(model).split(MIND_CURRENT)[-1]
    results = [m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)]
    assert not results[-1].is_error and len(model.checkpoint_requests) == 2  # applied; no repair
    row = [row for row in await store.load("session") if row.kind == "compaction"][-1]
    assert row.payload["mind"] == FIRST_MIND.replace(MIND_PLACEHOLDERS["## Workstreams"], "- the parser fix: in progress")


async def test_a_later_checkpoint_starts_from_the_mind_and_memo_it_left_and_needs_one_request_when_nothing_changed(
    tmp_path,
):
    models, (first, second), store = await _consolidate(tmp_path, [_fill_mind(tmp_path), [[Done(assistant(CHECKPOINT))]]])

    async def _store_rows(_):
        return await store.load("session")

    request = _request(models[1])
    assert MIND_FILE_PREVIOUS in request
    # The first crossing's input: in the first checkpoint's tail, moved out by the second. The mind follows, whole.
    seq = next(row.context_seq for row in await _store_rows(models) if row.row_id == "cross0")
    assert request.endswith(f"Current memo.md:\n- [x] (#1) typed input\n- [ ] (#{seq}) typed cross0\n\n"
                            f"{MIND_CURRENT}\n{first.payload['mind']}\n</context-checkpoint-request>")
    assert len(models[1].checkpoint_requests) == 1  # no tool, no repair: one request
    # What the turn left alone stays word for word: the mind, and the message it carried; one left unchecked goes.
    assert second.payload["mind"] == FIRST_MIND
    assert second.payload["memo"] == first.payload["memo"]
    assert second.payload["summary"].count(MISSION) == 1


async def test_a_mind_that_lost_its_structure_gets_one_repair_and_then_the_previous_mind_stays(tmp_path):
    scratch = tmp_path / "scratch"
    broken = [
        _batch(_edit("m", scratch / MIND_FILE, (f"## Mission\n{MISSION}\n", ""))),
        [Done(assistant(CHECKPOINT))],
        [Done(assistant(CHECKPOINT))],  # after the repair, still broken
    ]
    models, (first, second), store = await _consolidate(tmp_path, [_fill_mind(tmp_path), broken])
    repair = models[1].checkpoint_requests[-1].messages[-1].content[0].text
    assert 'mind.md lost its structure: the heading line "## Mission" is missing' in repair
    assert len(models[1].checkpoint_requests) == 3
    assert second.payload["mind"] == FIRST_MIND  # the previous mind, as it was
    audit = [audit for _, _, audit in store.audits][-1]
    assert audit["outcome"] == "completed" and audit["repair"].startswith("mind.md lost its structure")


async def test_a_mind_past_a_section_limit_is_kept_whole_shown_cut_and_given_whole_to_the_next_checkpoint(tmp_path):
    # On ROOMY's 32,000-token window the limits are a sixth of the user's (section 7): Goals 333 tokens, revert 2,000.
    assert mind_limits(32_000) == (166, 333, 250, 250) and mind_revert(32_000) == 1_998
    scratch = tmp_path / "scratch"
    workstreams = "\n".join(f"- workstream {index}: " + "w" * 60 for index in range(30))  # ~600 tokens
    grown = [
        _batch(_edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Workstreams"], workstreams))),
        [Done(assistant(CHECKPOINT))],
        [Done(assistant(CHECKPOINT))],  # the repair asked to condense it; it did not
    ]
    # The third turn leaves it as it is: the whole mind is still past the limit, so it is asked to condense once more.
    again = [[Done(assistant(CHECKPOINT))], [Done(assistant(CHECKPOINT))]]
    models, (_, second, third), _ = await _consolidate(tmp_path, [_fill_mind(tmp_path), grown, again])
    repair = models[1].checkpoint_requests[-1].messages[-1].content[0].text
    assert "mind.md is over its length limits: Goals is about" in repair and "at most 333" in repair
    whole = FIRST_MIND.replace(MIND_PLACEHOLDERS["## Workstreams"], workstreams)
    assert (second.payload["mind"], second.payload["mind_truncated"]) == (whole, True)
    copies = list((tmp_path / "checkpoints").iterdir())
    assert len(copies) == 1 and copies[0].read_text() == whole and not os.access(copies[0], os.W_OK)
    summary = second.payload["summary"]
    assert MIND_CUT.format(path=str(copies[0])) in summary and "- workstream 29" not in summary and MISSION in summary
    # The next turn starts from the whole mind, and its request gives it whole, the cut section too.
    request = _request(models[2])
    assert MIND_FILE_PREVIOUS in request and request.endswith(f"{MIND_CURRENT}\n{whole}\n</context-checkpoint-request>")
    assert third.payload["mind"] == whole


async def test_a_mind_piled_past_twice_its_budget_is_not_kept(tmp_path):
    scratch = tmp_path / "scratch"
    lessons = "\n".join(f"- lesson {index}: " + "l" * 400 for index in range(130))  # ~13,000 tokens
    assert text_tokens(lessons) > mind_revert()  # past twice the user's 6,000, on any window
    piled = [
        _batch(_edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["# 4. Lessons"], lessons))),
        [Done(assistant(CHECKPOINT))],
        [Done(assistant(CHECKPOINT))],
    ]
    _, (_, second), _ = await _consolidate(tmp_path, [_fill_mind(tmp_path), piled])
    assert (second.payload["mind"], second.payload["mind_truncated"]) == (FIRST_MIND, False)
    assert not (tmp_path / "checkpoints").exists()


async def test_a_memo_changed_beyond_its_checkboxes_keeps_every_candidate(tmp_path):
    scratch = tmp_path / "scratch"
    garbled = [
        _batch(_edit("n", scratch / MEMO_FILE, ("typed input", "the user said to fix it"))),
        [Done(assistant(CHECKPOINT))],
        [Done(assistant(CHECKPOINT))],
    ]
    (model,), (row,), _ = await _consolidate(tmp_path, [garbled])
    repair = model.checkpoint_requests[-1].messages[-1].content[0].text
    assert "memo.md was changed beyond its checkboxes" in repair
    assert row.payload["memo"] == [{"seq": 1, "text": "typed input", "truncated": False}]  # never fewer


async def test_checked_messages_past_the_memo_budget_are_all_kept_each_cut_to_a_common_share(tmp_path):
    store = InMemoryTranscriptStore()
    typed = {f"turn{index}": f"rule {index}: " + "z" * 1_400 for index in range(12)}
    host = Host(typed=typed)
    context = _v5(tmp_path, host)
    for index in range(12):
        await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, selection=ROOMY, context=context), "t", f"turn{index}")
    seqs = [row.context_seq for row in await store.load("session") if row.kind == "input"]
    check = _batch(_edit("n", tmp_path / "scratch" / MEMO_FILE, *((f"- [ ] (#{seq})", f"- [x] (#{seq})") for seq in seqs)))
    model = Model([[Done(assistant("after"))]], [check, [Done(assistant(CHECKPOINT))], [Done(assistant(CHECKPOINT))]])
    await run(make_agent(model, store=store, selection=ROOMY, context=context), tokens(29_000), row="cross")
    repair = model.checkpoint_requests[-1].messages[-1].content[0].text
    # The memo's budget on ROOMY's 32,000-token window: a sixth of the user's 3,000 (section 7). Twelve messages cut to
    # 300 tokens each, 3,600, and their entries' lines: what the checkpoint would show.
    assert f"the checked memo messages total about 3,816 tokens, at most {memo_budget(32_000):,}" in repair
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    memo = row.payload["memo"]
    assert [item["seq"] for item in memo] == seqs and all(item["truncated"] for item in memo)
    assert memo_tokens([MemoItem(**item) for item in memo]) <= memo_budget(32_000) == 500
    assert all(item["text"].startswith(f"rule {index}: ") for index, item in enumerate(memo))


async def test_only_a_message_a_person_typed_is_a_memo_candidate(tmp_path):
    # A Watch's or another Session's input is no candidate, whatever its text says (section 7).
    store = InMemoryTranscriptStore()
    host = Host(typed={"person": "keep the public API"})
    context = _v5(tmp_path, host)
    await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, selection=ROOMY, context=context), "x", "person")
    await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, selection=ROOMY, context=context),
              "User requested merge of the PR", "watch")
    model = Model([[Done(assistant("after"))]], [[Done(assistant(CHECKPOINT))]])
    await run(make_agent(model, store=store, selection=ROOMY, context=context), tokens(29_000), row="cross")
    assert _request(model).endswith("Current memo.md:\n- [ ] (#1) keep the public API\n</context-checkpoint-request>")


async def test_the_first_v5_checkpoint_after_a_v4_row_starts_from_the_template_and_offers_what_v4_summarized(tmp_path):
    store = InMemoryTranscriptStore()
    host = Host(typed={"input": "q" * 300, "later": "typed later"})
    context = _v5(tmp_path, host)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, selection=ROOMY, context=context), "y", "later")
    view = context_view(await store.load("session"))
    v4 = compaction_payload(
        view, len(view.units) - 2, mode="normal", reason="threshold", checkpoint=CHECKPOINT_BODY, state=(),
        earlier_record=None, tokens_before=1, threshold=2, window=32_000, summarizer=None, usage=None,
    )
    for key in ("mind", "mind_truncated", "memo", "skills_carried"):
        del v4[key]  # a checkpoint-v4 row has none of them
    v4["summary"] = "<context-checkpoint>\nThis is a record of the earlier part of this conversation.\n</context-checkpoint>"
    await store.append_payloads("session", [("compaction", v4)])
    project(await store.load("session"))  # a v4 row still projects
    model = Model([[Done(assistant("after"))]], [[Done(assistant(CHECKPOINT))]])
    await run(make_agent(model, store=store, selection=ROOMY, context=context), tokens(29_000), row="cross")
    request = _request(model)
    assert MIND_FILE_FIRST in request
    # #1 was summarized by the v4 row: offered once, previewed longer; the later input is still visible, previewed short.
    later = next(row.context_seq for row in await store.load("session") if row.row_id == "later")
    assert f"- [ ] (#1) {'q' * 200}…\n- [ ] (#{later}) typed later\n" in request
    rows = await store.load("session")
    assert [row.kind for row in rows].count("compaction") == 2
    project(rows)


async def test_a_host_that_cannot_say_who_typed_what_fails_the_checkpoint_before_any_model_call(tmp_path):
    class Blind(Host):
        def user_messages(self, row_ids):
            raise RuntimeError("messages table locked")

    store = InMemoryTranscriptStore()
    context = _v5(tmp_path, Blind())
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    model = Model([[Done(assistant("after"))]], [])
    events = await cross(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    assert not model.checkpoint_requests and events[-1].reason == "completed"
    (failed,) = [event for event in events if isinstance(event, CompactionFailed)]
    assert "the host failed: RuntimeError: messages table locked" in failed.error
    assert [audit["outcome"] for _, _, audit in store.audits] == ["failed"]


async def test_a_host_that_cannot_say_who_typed_what_still_lets_the_ladder_drop(tmp_path):
    # The context cannot fit and the checkpoint fails on the host; the ladder's drop cannot gather the memo it would
    # carry, so it drops without it rather than stop the run (section 8 c).
    class Blind(Host):
        def user_messages(self, row_ids):
            raise RuntimeError("messages table locked")

    store = InMemoryTranscriptStore()
    await _history_on_a_large_window(store, [5_000] * 12)
    model = Model([[Done(assistant("done"))]], [])
    events = await run(make_agent(model, store=store, context=_v5(tmp_path, Blind())))
    assert not model.checkpoint_requests and events[-1].reason == "completed"
    assert any(isinstance(event, CompactionFailed) for event in events)
    drops = [row for row in await store.load("session") if row.kind == "compaction"]
    assert drops and all(row.payload["mode"] == "dropped" and row.payload["memo"] == [] for row in drops)


async def test_a_drop_after_a_v4_row_carries_its_skills_rendered_now(tmp_path):
    # A checkpoint-v4 row names its skills only. A drop that keeps it carries them as a checkpoint would: rendered by
    # the host now (section 8 c), not lost with the summary the drop replaces.
    store = InMemoryTranscriptStore()
    body = '<skill_content name="review">\nRead the whole diff first.\n</skill_content>'
    host = Host(skills={"review": body})
    context = _v5(tmp_path, host)
    await _history_on_a_large_window(store, [5_000] * 4)
    view = context_view(await store.load("session"))
    v4 = compaction_payload(
        view, len(view.units) - 1, mode="normal", reason="threshold", checkpoint=CHECKPOINT_BODY, state=(),
        earlier_record=None, tokens_before=1, threshold=2, window=1_000_000, summarizer=None, usage=None,
    )
    for key in ("mind", "mind_truncated", "memo", "skills_carried"):
        del v4[key]
    v4["skills"] = [{"name": "review"}]
    await store.append_payloads("session", [("compaction", v4)])
    await _history_on_a_large_window(store, [5_000] * 12, row="more")
    model = Model([[Done(assistant("done"))]], [[ProviderError("server", "down", False)]])
    events = await run(make_agent(model, store=store, context=context))
    assert events[-1].reason == "completed"
    _, first, *_ = [row for row in await store.load("session") if row.kind == "compaction"]
    assert first.payload["mode"] == "dropped" and first.payload["checkpoint"] == CHECKPOINT_BODY
    (carried,) = first.payload["skills_carried"]
    assert (carried["name"], carried["content"]) == ("review", body) and ["review"] in host.rendered
    assert f"<skills>\n{SKILLS_LEAD_CARRIED}\n{body}\n</skills>" in first.payload["summary"]


async def test_the_skills_a_checkpoint_takes_out_come_back_as_they_read_now(tmp_path):
    store = InMemoryTranscriptStore()
    body = '<skill_content name="review">\nRead the whole diff first.\n</skill_content>'
    host = Host(skills={"review": body})
    context = _v5(tmp_path, host)
    loader = FakeTool("bash", result=ToolResult((text(body),), details={"skills": [{"name": "review"}]}))
    model = Model([call("bash", "load", command="vibe skill load review"), *history()])
    await run(make_agent(model, store=store, tools=[loader, reader(tokens(3_000))], selection=ROOMY, context=context))
    model = Model([[Done(assistant("after"))]], [[Done(assistant(CHECKPOINT))]])
    await cross(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    assert row.payload["skills"] == [{"name": "review"}] and host.rendered == [["review"]]  # one call per checkpoint
    (carried,) = row.payload["skills_carried"]
    assert (carried["name"], carried["revision"], carried["content"]) == ("review", "rev-review", body)
    assert f"<skills>\n{SKILLS_LEAD_CARRIED}\n{body}\n</skills>" in row.payload["summary"]


async def test_the_turn_s_own_files_are_revised_only_with_edit(tmp_path):
    scratch = tmp_path / "scratch"
    rewrite = ToolCallBlock("w", "write", {"path": str(scratch / MIND_FILE), "content": "# my own mind"})
    other = ToolCallBlock("o", "write", {"path": str(scratch / "notes.md"), "content": "free"})
    script = [_batch(rewrite, other), [Done(assistant(CHECKPOINT))]]
    (model,), (row,), _ = await _consolidate(tmp_path, [script])
    results = [m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)][-2:]
    assert results[0].is_error and results[0].content == (text(DREAMING.rewrite_denied.format(name=MIND_FILE)),)
    assert not results[1].is_error and (scratch / "notes.md").read_text() == "free"
    assert row.payload["mind"] == ""  # the template untouched: no mind


async def test_without_a_scratch_root_the_previous_mind_stays_and_every_message_is_kept(tmp_path):
    store = InMemoryTranscriptStore()
    context = ContextConfig(host=Host())  # no scratch directory: no files (section 6)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    model = Model([[Done(assistant("after"))]], [[Done(assistant(CHECKPOINT))]])
    await cross(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    assert row.payload["mind"] == "" and row.payload["memo"] == [{"seq": 1, "text": "typed input", "truncated": False}]
    assert len(model.checkpoint_requests) == 1


async def test_a_drop_carries_the_mind_skills_and_every_message(tmp_path):
    store = InMemoryTranscriptStore()
    host = Host()
    # Room for the edits at the first checkpoint (section 6): its input limit is past the window's 0.9.
    medium = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=70_000, input_limit=100_000))
    await _history_on_a_large_window(store, [5_000] * 13)
    scratch = tmp_path / "scratch"
    fill = [
        _batch(
            _edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Mission"], MISSION)),
            _edit("n", scratch / MEMO_FILE, ("- [ ] (#1)", "- [x] (#1)")),
        ),
        [Done(assistant(CHECKPOINT))],
    ]
    first = make_agent(Model([[Done(assistant("noted"))]], fill), store=store, selection=medium, context=_v5(tmp_path, host))
    assert (await run(first, row="first"))[-1].reason == "completed"
    await _history_on_a_large_window(store, [5_000] * 4, row="more")
    model = Model([[Done(assistant("done"))]], [[ProviderError("server", "down", False)]])
    events = await run(make_agent(model, store=store, context=_v5(tmp_path, host)))
    first_row, *drops = [row for row in await store.load("session") if row.kind == "compaction"]
    assert drops and events[-1].reason == "completed"
    for dropped in drops:
        assert dropped.payload["mind"] == FIRST_MIND and dropped.payload["checkpoint"] == CHECKPOINT_BODY
        seqs = [item["seq"] for item in dropped.payload["memo"]]
        assert seqs[0] == 1 and set(seqs) > {1}  # the carried message, and every one the drop moved out
        assert MISSION in dropped.payload["summary"]


async def test_a_memo_the_turn_had_no_room_to_check_keeps_every_candidate(tmp_path):
    # The turn's five rounds go to reads (section 6): its edit of memo.md gets the used-up budget's text, so its
    # untouched memo.md says nothing about what to drop.
    store = InMemoryTranscriptStore()
    context = _v5(tmp_path)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    check = _batch(_edit("n", tmp_path / "scratch" / MEMO_FILE, ("- [ ] (#1)", "- [x] (#1)")))
    script = [*(call("read", f"peek{index}", path="f") for index in range(5)), check, [Done(assistant(CHECKPOINT))]]
    model = Model([[Done(assistant("after"))]], script)
    await cross(make_agent(model, store=store, tools=[reader("short")], selection=ROOMY, context=context))
    assert model.checkpoint_requests[-1].messages[-1].content == (text(BUDGET_USED),)
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    assert row.payload["memo"] == [{"seq": 1, "text": "typed input", "truncated": False}]


async def test_a_scratch_edit_runs_with_less_room_than_a_read_needs(tmp_path):
    # Crossing T at ~24,000 tokens of a 32,000-token limit leaves under the 4,000-token floor of a read, not under a
    # scratch edit's: its result is one short line, never cut (section 6). A first mind can be filled there.
    store = InMemoryTranscriptStore()
    context = _v5(tmp_path)
    await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, context=context), "keep the API", "person")
    scratch = tmp_path / "scratch"
    batch = _batch(
        _edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Mission"], MISSION)),
        ToolCallBlock("r", "read", {"path": "f"}),
    )
    model = Model(reads(4), [batch, [Done(assistant(CHECKPOINT))]])
    await run(make_agent(model, store=store, tools=[reader(tokens(6_000))], context=context))
    edited, read = [m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)][-2:]
    assert not edited.is_error and edited.content[0].text.startswith("Successfully replaced 1 block(s)")
    assert read.content == (text(BUDGET_USED),)
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    assert row.payload["mind"] == FIRST_MIND


async def test_a_mind_copy_is_published_whole_or_not_at_all(tmp_path, monkeypatch):
    # A copy that fails mid-write leaves nothing at its name, so a later checkpoint of the same mind never points to a
    # partial copy (section 10).
    scratch = tmp_path / "scratch"
    workstreams = "\n".join(f"- workstream {index}: " + "w" * 60 for index in range(30))  # past Goals' 333 on ROOMY
    grown = [
        _batch(_edit("m", scratch / MIND_FILE, (MIND_PLACEHOLDERS["## Workstreams"], workstreams))),
        [Done(assistant(CHECKPOINT))],
        [Done(assistant(CHECKPOINT))],
    ]
    real_fdopen = os.fdopen

    def path_of(fd) -> str:
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            import fcntl

            return fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0].decode()

    class FullDisk:
        """A file under checkpoints/ whose disk fills after its first bytes."""

        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            self.handle.write(data[:10])
            self.handle.flush()
            raise OSError(28, "No space left on device")

    def fdopen(fd, *args, **options):
        handle = real_fdopen(fd, *args, **options)
        return FullDisk(handle) if "checkpoints" in path_of(fd) else handle

    monkeypatch.setattr("core.agent_core.agent.loop.os.fdopen", fdopen)
    store = InMemoryTranscriptStore()
    context = _v5(tmp_path)
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    model = Model([[Done(assistant("after"))]], grown)
    events = await cross(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context))
    (failed,) = [event for event in events if isinstance(event, CompactionFailed)]
    assert "No space left on device" in failed.error
    assert not [row for row in await store.load("session") if row.kind == "compaction"]
    assert not list((tmp_path / "checkpoints").iterdir())  # no partial copy, no temporary file
    monkeypatch.setattr("core.agent_core.agent.loop.os.fdopen", real_fdopen)
    model = Model([[Done(assistant("after"))]], grown)
    await cross(make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=ROOMY, context=context), "again")
    (row,) = [row for row in await store.load("session") if row.kind == "compaction"]
    (copy,) = (tmp_path / "checkpoints").iterdir()
    assert copy.read_text() == row.payload["mind"] and row.payload["mind_truncated"]


async def test_the_turn_is_given_its_files_word_for_word_and_an_edit_written_as_a_text_file_matches(tmp_path):
    # A replay of a real Session read both files before editing, because the request described the template instead
    # of giving it, and its first edit missed: it ended the whole template with a newline, which the file lacked.
    scratch = tmp_path / "scratch"
    whole = _batch(_edit("m", scratch / MIND_FILE, (MIND_TEMPLATE + "\n", FIRST_MIND + "\n")))
    (model,), (row,), _ = await _consolidate(tmp_path, [[whole, [Done(assistant(CHECKPOINT))]]])
    request = _request(model)
    assert "you already have both word for word, so do not read them" in request
    assert f"so it is this empty template, word for word:\n{MIND_TEMPLATE}\n" in request
    edited = [m for m in model.checkpoint_requests[-1].messages if isinstance(m, ToolResultMessage)][-1]
    assert not edited.is_error and row.payload["mind"] == FIRST_MIND  # stored without the file's final newline


#: A 32,000-token window answering 4,096: T = 23,904 (section 1).
SMALL32 = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=32_000, input_limit=None))


async def test_on_a_small_route_a_checkpoint_leaves_the_context_much_smaller(tmp_path):
    # A Session whose last checkpoint was written on a large route, its mind and memo at the user's full limits
    # (6,000 and 3,000 tokens), moves to a 32,000-token route. Its next checkpoint fits them to this route's limits
    # (section 7), so the context it leaves is well under the threshold: no ineffective checkpoint, no loop.
    store = InMemoryTranscriptStore()
    typed = {f"turn{index}": f"rule {index}: " + "z" * 1_196 for index in range(10)}
    context = _v5(tmp_path, Host(typed=typed))
    for index in range(10):
        await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, context=context), "t", f"turn{index}")
    view = context_view(await store.load("session"))
    lines = lambda n, width: "\n".join(f"- item {i}: " + "m" * width for i in range(n))  # noqa: E731
    mind = (
        MIND_TEMPLATE.replace(MIND_PLACEHOLDERS["## Agreements"], f"{lines(12, 300)}")
        .replace(MIND_PLACEHOLDERS["## Mission"], f"{MISSION}\n{lines(20, 350)}")
        .replace(MIND_PLACEHOLDERS["# 3. Key decisions"], f"{lines(18, 300)}")
        .replace(MIND_PLACEHOLDERS["# 4. Lessons"], f"{lines(18, 300)}")
    )
    assert review_mind(mind, mind).over == () and text_tokens(mind) > 5_000  # within the user's limits
    memo = tuple(MemoItem(unit.seq, typed[unit.lead.row_id]) for unit in view.units if unit.lead.kind == "input")
    large = compaction_payload(
        view, len(view.units) - 1, mode="normal", reason="threshold", checkpoint=CHECKPOINT_BODY, state=(),
        earlier_record=None, tokens_before=1, threshold=2, window=1_000_000, summarizer=None, usage=None,
        mind=mind, memo=memo,
    )
    assert sum(text_tokens(item["text"]) for item in large["memo"]) >= 2_990
    await store.append_payloads("session", [("compaction", large)])
    # On the small route two turns stay under T; the third crosses it, and its checkpoint moves the two out.
    for row in ("mid1", "mid2"):
        await run(make_agent(Model([[Done(assistant("ok"))]]), store=store, selection=SMALL32, context=context), tokens(6_500), row)
    model = Model([[Done(assistant("after"))]], [[Done(assistant(CHECKPOINT))], [Done(assistant(CHECKPOINT))]])
    events = await run(make_agent(model, store=store, selection=SMALL32, context=context), tokens(3_000), "cross")
    (finished,) = [event for event in events if isinstance(event, CompactionFinished)]
    threshold = 23_904
    assert finished.tokens_before >= threshold and events[-1].reason == "completed"
    after = context_view(await store.load("session"))
    shown = message_tokens(after.checkpoint)
    # The checkpoint itself takes a small part of this route's threshold, and the context it leaves is far below the
    # ineffective line (0.75 * T): the next requests have room, and no checkpoint follows at once.
    assert shown < 0.15 * threshold and finished.tokens_after_estimate < 0.5 * threshold
    row = [row for row in await store.load("session") if row.kind == "compaction"][-1]
    assert row.payload["mind"] == mind and row.payload["mind_truncated"]  # kept whole, shown cut to this route
    assert memo_tokens([MemoItem(**item) for item in row.payload["memo"]]) <= memo_budget(32_000)
    assert [item["seq"] for item in row.payload["memo"]] == [item.seq for item in memo]  # every message kept
    repair = model.checkpoint_requests[-1].messages[-1].content[0].text
    assert "mind.md is over its length limits" in repair and "at most 1,000" not in repair  # per section, scaled
