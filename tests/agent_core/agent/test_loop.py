"""C1-C6/C9 through the recording provider, not private loop state.

Foundation coverage only round-trips messages. These tests protect execution
boundaries absent there: per-request tools, durable-vs-transient context, hook
policy, queue finality, fork/resume state, cancellation, and commit/event order.
Gates use Events instead of timing assumptions.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from core.agent_core.agent.events import (
    AgentError,
    AssistantTextDelta,
    AssistantThinkingDelta,
    MessageCommitted,
    RunEnded,
    RunStarted,
    SteerApplied,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)
from core.agent_core.agent.hooks import (
    AlterArgs,
    AlterResult,
    Deny,
    End,
    Hooks,
    RunSetup,
    SkipTools,
)
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import RetryPolicy
from core.agent_core.ai.provider import (
    Done,
    ModelCapabilities,
    ModelRequest,
    ProviderError,
    TextDelta,
    ThinkingDelta,
    ToolCallStart,
)
from core.agent_core.agent.models import ModelSelection
from core.agent_core.harness.context import ContextConfig, budget
from core.agent_core.harness.projection import project
from core.agent_core.messages import AssistantMessage, ThinkingBlock, ToolCallBlock, ToolResultMessage, Usage, UserMessage, text
from core.agent_core.tools.base import ToolResult
from tests.agent_core.fakes import (
    FakeJobHost,
    FakeModelRouter,
    FakeTool,
    InMemoryTranscriptStore,
    ScriptedProvider,
    ENDPOINT,
    assistant,
    input_row,
    user,
)


def make_agent(provider, *, store=None, session_id="session", tools=(), hooks=(), jobs=None, **options):
    return Agent(
        session_id=session_id,
        models=FakeModelRouter(provider),
        tools=tools,
        hooks=hooks,
        store=store or InMemoryTranscriptStore(),
        jobs=jobs or FakeJobHost(),
        cwd="/test-owned",
        **options,
    )


async def collect(agent, row_id="input", value="hello", turn_id="turn"):
    return [event async for event in agent.run(input_row(row_id, value), turn_id=turn_id)]


def user_texts(request):
    return [message.content[0].text for message in request.messages if isinstance(message, UserMessage)]


async def test_C1_tool_set_is_captured_per_request_and_unavailable_calls_are_errors():
    old, new = FakeTool("old"), FakeTool("new")
    entered, release = asyncio.Event(), asyncio.Event()

    async def first(request, cancel):
        entered.set()
        await release.wait()
        yield Done(assistant(calls=[ToolCallBlock("a", "old"), ToolCallBlock("b", "new")]))

    provider = ScriptedProvider(
        [
            first,
            [Done(assistant(calls=[ToolCallBlock("c", "new"), ToolCallBlock("d", "old")]))],
            [Done(assistant())],
        ]
    )
    agent = make_agent(provider, tools=[old])
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    agent.set_tools([new])
    release.set()
    await run
    assert [[spec.name for spec in request.tools] for request in provider.requests] == [["old"], ["new"], ["new"]]
    assert [ctx.tool_call_id for _, ctx in old.calls] == ["a"]
    assert [ctx.tool_call_id for _, ctx in new.calls] == ["c"]
    results = [row.message for row in await agent.store.load("session") if row.kind == "tool_result"]
    assert [result.tool_call_id for result in results] == ["a", "b", "c", "d"]
    assert [(result.is_error, result.content[0].text) for result in (results[1], results[3])] == [
        (True, "Tool new is not available."),
        (True, "Tool old is not available."),
    ]


async def test_C2_rewrites_compose_on_detached_requests_and_select_rewritten_transport():
    class Rewrite(Hooks):
        async def before_run(self, input, ctx):
            return RunSetup(system="base")

        async def before_model(self, request, ctx):
            # Nested ToolCallBlock.arguments is mutable despite frozen dataclasses.
            for message in request.messages:
                for call in getattr(message, "tool_calls", ()):
                    call.arguments["secret"] = "request only"
            return replace(
                request,
                system=request.system + "/one",
                messages=request.messages + (user("transient"),),
                tools=(),
                endpoint=replace(request.endpoint, protocol="google"),
                max_tokens=99,
            )

    class Second(Hooks):
        async def before_model(self, request, ctx):
            assert request.system == "base/one"
            return replace(request, system=request.system + "/two")

    store = InMemoryTranscriptStore()
    await store.append_response(
        "session", assistant(calls=[ToolCallBlock("past", "echo", {"secret": "stored"})]), final=False
    )
    await store.append_tool_result("session", ToolResultMessage("past", "echo", (text("old result"),)), details={})
    provider = ScriptedProvider([[Done(assistant())]])
    agent = make_agent(provider, store=store, hooks=[Rewrite(), Second()], tools=[FakeTool()])
    await collect(agent)
    request = provider.requests[0]
    assert (request.system, request.max_tokens, request.tools) == ("base/one/two", 99, ())
    assert user_texts(request) == ["hello", "transient"]
    assert agent.models.protocols == ["google"]
    rows = await store.load("session")
    assert rows[0].message.tool_calls[0].arguments == {"secret": "stored"}
    assert [row.message for row in rows if row.kind == "input"] == [user("hello")]


async def test_C3_deny_and_argument_result_rewrites_compose_in_registration_order():
    order = []

    class First(Hooks):
        async def before_tool(self, call, ctx):
            order.append(("first", call.id))
            if call.id == "denied":
                return Deny("policy reason")
            return AlterArgs({"value": 2})

        async def after_tool(self, call, result, ctx):
            return AlterResult(replace(result, content=(text(result.content[0].text + "/first"),)))

    class Second(Hooks):
        async def before_tool(self, call, ctx):
            order.append(("second", call.id))
            return AlterArgs({"value": call.arguments["value"] + 3})

        async def after_tool(self, call, result, ctx):
            return AlterResult(replace(result, content=(text(result.content[0].text + "/second"),)))

    tool = FakeTool()
    provider = ScriptedProvider(
        [
            [Done(assistant(calls=[ToolCallBlock("denied", "echo"), ToolCallBlock("allowed", "echo")]))],
            [Done(assistant())],
        ]
    )
    agent = make_agent(provider, tools=[tool], hooks=[First(), Second()])
    await collect(agent)
    assert order == [("first", "denied"), ("first", "allowed"), ("second", "allowed")]
    assert [args for args, _ in tool.calls] == [{"value": 5}]
    results = [m for m in provider.requests[1].messages if isinstance(m, ToolResultMessage)]
    assert [m.content[0].text for m in results] == ["policy reason/first/second", "tool result/first/second"]
    assert results[0].is_error


@pytest.mark.parametrize("where", ["before_model", "after_model", "before_tool", "after_tool"])
async def test_C3_end_commits_the_step_settles_unexecuted_calls_and_stops_hook_chain(where):
    hooks_seen = []

    class Stop(Hooks):
        async def before_model(self, request, ctx):
            if where == "before_model":
                ctx.state["stopped"] = True
                return End()

        async def after_model(self, message, ctx):
            if where == "after_model":
                return End()

        async def before_tool(self, call, ctx):
            if where == "before_tool":
                return End()

        async def after_tool(self, call, result, ctx):
            if where == "after_tool":
                return End()

    class Later(Hooks):
        async def before_model(self, request, ctx):
            hooks_seen.append("before_model")

        async def after_model(self, message, ctx):
            hooks_seen.append("after_model")

        async def before_tool(self, call, ctx):
            hooks_seen.append("before_tool")

        async def after_tool(self, call, result, ctx):
            hooks_seen.append("after_tool")

    tool = FakeTool()
    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("a", "echo"), ToolCallBlock("b", "echo")]))]])
    agent = make_agent(provider, tools=[tool], hooks=[Stop(), Later()])
    events = await collect(agent)
    assert events[-1].reason == "ended_by_hook"
    assert where not in hooks_seen
    assert len(tool.calls) == (1 if where == "after_tool" else 0)
    rows = await agent.store.load("session")
    assert len([row for row in rows if row.kind == "tool_result"]) == (0 if where == "before_model" else 2)
    if where == "before_model":
        assert agent.snapshot().state == {"stopped": True}


async def test_C3_skip_tools_and_terminate_have_distinct_batch_semantics():
    class Skip(Hooks):
        async def after_model(self, message, ctx):
            return SkipTools()

    tool = FakeTool(result=ToolResult((text("terminate"),), terminate=True))
    response = assistant(calls=[ToolCallBlock("a", "echo"), ToolCallBlock("b", "echo")])
    skipped = ScriptedProvider([[Done(response)], [Done(assistant())]])
    await collect(make_agent(skipped, tools=[tool], hooks=[Skip()]))
    assert tool.calls == []
    assert [m.content[0].text for m in skipped.requests[1].messages if isinstance(m, ToolResultMessage)] == [
        "[skipped by policy]",
        "[skipped by policy]",
    ]
    provider = ScriptedProvider([[Done(response)]])
    agent = make_agent(provider, tools=[tool])
    events = await collect(agent)
    assert len(tool.calls) == 2
    assert events[-1].reason == "completed"
    assert len([row for row in await agent.store.load("session") if row.kind == "tool_result"]) == 2


@pytest.mark.parametrize(
    ("stop_reason", "run_reason"),
    [("safety", "error"), ("error", "error"), ("aborted", "aborted")],
)
async def test_non_tool_stop_settles_calls_without_executing_tools(stop_reason, run_reason):
    tool = FakeTool()
    response = assistant(
        calls=[ToolCallBlock("blocked-a", "echo"), ToolCallBlock("blocked-b", "echo")],
        stop_reason=stop_reason,
    )
    agent = make_agent(ScriptedProvider([[Done(response)]]), tools=[tool])

    events = await collect(agent)
    rows = await agent.store.load("session")
    results = [row.message for row in rows if row.kind == "tool_result"]

    assert tool.calls == []
    assert len(results) == 2
    assert all(result.is_error for result in results)
    assert all(stop_reason in result.content[0].text for result in results)
    assert events[-1].reason == run_reason


async def test_length_stop_settles_calls_and_retries_the_model_turn():
    tool = FakeTool()
    provider = ScriptedProvider(
        [
            [Done(assistant(calls=[ToolCallBlock("truncated", "echo", {"x": 1})], stop_reason="length"))],
            [Done(assistant("reissued"))],
        ]
    )

    events = await collect(make_agent(provider, tools=[tool]))

    assert tool.calls == []
    assert len(provider.requests) == 2
    results = [message for message in provider.requests[1].messages if isinstance(message, ToolResultMessage)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert "re-issue" in results[0].content[0].text
    assert events[-1].reason == "completed"


async def test_length_stop_honors_end_hook_after_settling_calls():
    class Stop(Hooks):
        async def after_model(self, message, ctx):
            return End()

    tool = FakeTool()
    provider = ScriptedProvider(
        [[Done(assistant(calls=[ToolCallBlock("truncated", "echo", {"x": 1})], stop_reason="length"))]]
    )
    agent = make_agent(provider, tools=[tool], hooks=[Stop()])

    events = await collect(agent)

    assert tool.calls == []
    assert len(provider.requests) == 1
    assert events[-1].reason == "ended_by_hook"
    assert len([row for row in await agent.store.load("session") if row.kind == "tool_result"]) == 1


async def test_length_stop_applies_steer_before_reissued_model_call():
    holder = {}

    async def first(request, cancel):
        assert await holder["agent"].steer(input_row("steer", "steered"))
        yield Done(assistant(calls=[ToolCallBlock("truncated", "echo", {"x": 1})], stop_reason="length"))

    provider = ScriptedProvider([first, [Done(assistant("done"))]])
    holder["agent"] = make_agent(provider, tools=[FakeTool()])

    events = await collect(holder["agent"])

    assert len(provider.requests) == 2
    assert user_texts(provider.requests[1]) == ["hello", "steered"]
    assert events[-1].reason == "completed"


async def test_length_stop_does_not_consume_steer_when_retry_budget_is_exhausted():
    holder = {}

    async def first(request, cancel):
        del request, cancel
        assert await holder["agent"].steer(input_row("steer", "preserve me"))
        yield Done(assistant(calls=[ToolCallBlock("truncated", "echo", {"x": 1})], stop_reason="length"))

    holder["agent"] = make_agent(
        ScriptedProvider([first]),
        tools=[FakeTool()],
        retry=RetryPolicy(max_retries=0, initial_delay_s=0),
    )
    events = await collect(holder["agent"])

    assert events[-1].reason == "error"
    pending = await holder["agent"].take_pending_inputs()
    assert [item.message.content[0].text for item in pending] == ["preserve me"]


async def test_repeated_length_tool_stops_are_bounded():
    tool = FakeTool()
    response = Done(assistant(calls=[ToolCallBlock("truncated", "echo", {"x": 1})], stop_reason="length"))
    provider = ScriptedProvider([[response], [response], [response]])

    events = await collect(make_agent(provider, tools=[tool], retry=RetryPolicy(initial_delay_s=0)))

    assert tool.calls == []
    assert len(provider.requests) == 3
    assert events[-1].reason == "error"
    assert [(event.kind, event.message) for event in events if isinstance(event, AgentError)] == [
        ("length", "The model repeatedly exceeded its output limit while emitting a tool call.")
    ]


async def test_C4_steer_waits_for_the_batch_and_follow_up_waits_for_natural_end():
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(arguments, ctx):
        if ctx.tool_call_id == "a":
            entered.set()
            await release.wait()
        return ToolResult((text(ctx.tool_call_id),))

    provider = ScriptedProvider(
        [
            [Done(assistant(calls=[ToolCallBlock("a", "echo"), ToolCallBlock("b", "echo")]))],
            [Done(assistant("intermediate"))],
            [Done(assistant("final"))],
        ]
    )
    agent = make_agent(provider, tools=[FakeTool(execute=execute)])
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    assert await agent.steer(input_row("steer", "改变方向"))
    assert await agent.follow_up(input_row("follow", "then follow up"))
    assert [row.kind for row in await agent.store.load("session")] == ["input", "response"]
    release.set()
    events = await run
    assert [user_texts(request) for request in provider.requests] == [
        ["hello"],
        ["hello", "改变方向"],
        ["hello", "改变方向", "then follow up"],
    ]
    rows = await agent.store.load("session")
    assert [row.kind for row in rows] == [
        "input",
        "response",
        "tool_result",
        "tool_result",
        "input",
        "response",
        "input",
        "response",
    ]
    assert [event.message_id for event in events if isinstance(event, SteerApplied)] == ["steer"]
    assert [agent.store.final[row.row_id] for row in rows if row.kind == "response"] == [False, False, True]


async def test_C4_pending_steer_prevents_finality_and_late_steer_is_refused_during_commit():
    class PausingStore(InMemoryTranscriptStore):
        def __init__(self):
            super().__init__()
            self.final_entered, self.final_release = asyncio.Event(), asyncio.Event()

        async def append_response(self, session_id, message, *, final):
            if final:
                self.final_entered.set()
                await self.final_release.wait()
            return await super().append_response(session_id, message, final=final)

    entered, release = asyncio.Event(), asyncio.Event()

    async def first(request, cancel):
        entered.set()
        await release.wait()
        yield Done(assistant("first"))

    store = PausingStore()
    provider = ScriptedProvider([first, [Done(assistant("last"))]])
    agent = make_agent(provider, store=store)
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    assert await agent.steer(input_row("pending", "one more"))
    release.set()
    await store.final_entered.wait()
    late = asyncio.create_task(agent.steer(input_row("late", "too late")))
    store.final_release.set()
    assert await late is False
    await run
    assert [user_texts(request) for request in provider.requests] == [["hello"], ["hello", "one more"]]
    assert [
        (row.message.content[0].text, store.final[row.row_id])
        for row in await store.load("session")
        if row.kind == "response"
    ] == [("first", False), ("last", True)]
    assert await agent.follow_up(input_row("late-follow", "later")) is False


async def test_C4_follow_up_stays_pending_while_a_steer_extends_a_tool_free_run():
    entered, release = asyncio.Event(), asyncio.Event()

    async def first(request, cancel):
        entered.set()
        await release.wait()
        yield Done(assistant())

    provider = ScriptedProvider([first, [Done(assistant())], [Done(assistant())]])
    agent = make_agent(provider)
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    assert await agent.follow_up(input_row("follow", "follow"))
    assert await agent.steer(input_row("steer", "steer"))
    release.set()
    await run
    assert [user_texts(request) for request in provider.requests] == [
        ["hello"],
        ["hello", "steer"],
        ["hello", "steer", "follow"],
    ]


async def test_before_run_setup_applies_to_one_run_and_after_run_state_is_durable():
    outcomes = []

    class Setup(Hooks):
        async def before_run(self, input, ctx):
            if input.message_id == "first":
                return RunSetup(system="first system", tools=(FakeTool("first"),))

        async def after_run(self, outcome, ctx):
            outcomes.append(outcome)
            ctx.state["finished"] = outcome.turn_id

    provider = ScriptedProvider([[Done(assistant())], [Done(assistant())]])
    agent = make_agent(provider, hooks=[Setup()], tools=[FakeTool("base")], system="base system")
    await collect(agent, "first", turn_id="first-turn")
    assert agent.snapshot().state == {"finished": "first-turn"}
    await collect(agent, "second", turn_id="second-turn")
    assert [(r.system, [t.name for t in r.tools]) for r in provider.requests] == [
        ("first system", ["first"]),
        ("base system", ["base"]),
    ]
    assert [outcome.reason for outcome in outcomes] == ["completed", "completed"]
    assert project(await agent.store.load("session")).state == {"finished": "second-turn"}


async def test_C5_fork_and_C6_resume_restore_persisted_state_without_shared_mutable_context():
    class State(Hooks):
        async def before_run(self, input, ctx):
            ctx.state.setdefault("history", []).append(input.message_id)

        async def before_model(self, request, ctx):
            return replace(request, system="/".join(ctx.state["history"]))

    store = InMemoryTranscriptStore()
    original = make_agent(ScriptedProvider([[Done(assistant("root"))]]), store=store, hooks=[State()])
    await collect(original, "root-input")
    snapshot = original.snapshot()
    for child in ("left", "right"):
        store.fork(snapshot, session_id=child)
    snapshot.state["history"].append("outside mutation")
    # Parent changes after the anchor must not leak into either child.
    await store.consume_input("session", "later-parent", user("parent only"))
    providers = {name: ScriptedProvider([[Done(assistant(name))]]) for name in ("left", "right")}
    await asyncio.gather(
        *(
            collect(make_agent(provider, store=store, session_id=name, hooks=[State()]), name, name)
            for name, provider in providers.items()
        )
    )
    for name, provider in providers.items():
        assert user_texts(provider.requests[0]) == ["hello", name]
        assert provider.requests[0].system == f"root-input/{name}"
    left_rows = await store.load("left")
    assert project(left_rows).state == {"history": ["root-input", "left"]}
    resumed_provider = ScriptedProvider([[Done(assistant("resumed"))]])
    resumed = make_agent(resumed_provider, store=store, session_id="left", hooks=[State()])
    await collect(resumed, "resume", "resume text")
    assert resumed_provider.requests[0].messages[:-1] == project(left_rows).messages
    assert resumed_provider.requests[0].system == "root-input/left/resume"


async def test_C9_events_reference_committed_rows_and_progress_precedes_tool_finish():
    async def execute(arguments, ctx):
        ctx.on_progress("working")
        return ToolResult((text("ready"),))

    provider = ScriptedProvider(
        [
            [
                ThinkingDelta(0, "think"),
                TextDelta(1, "text"),
                Done(assistant(calls=[ToolCallBlock("a", "echo", {"path": "文件"})])),
            ],
            [Done(assistant())],
        ]
    )
    agent = make_agent(provider, tools=[FakeTool(execute=execute)])
    events = []
    async for event in agent.run(input_row("input", "hello"), turn_id="the-turn-id"):
        rows = await agent.store.load("session")
        if isinstance(event, MessageCommitted):
            assert any(row.row_id == event.message_id and row.context_seq == event.context_seq for row in rows)
        if isinstance(event, ToolFinished):
            assert any(row.row_id == event.event_id for row in rows)
        events.append(event)
    assert [type(event) for event in events] == [
        RunStarted,
        AssistantThinkingDelta,
        AssistantTextDelta,
        MessageCommitted,
        ToolStarted,
        ToolProgress,
        ToolFinished,
        MessageCommitted,
        RunEnded,
    ]
    assert [event.seq for event in events] == list(range(len(events)))
    assert {event.turn_id for event in events} == {"the-turn-id"}
    assert len(next(event for event in events if isinstance(event, ToolStarted)).preview) <= 500


@pytest.mark.parametrize("streamed", [False, "text", "tool", "partial"])
async def test_retry_obeys_provider_flag_and_allows_usage_only_partial(streamed, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", sleep)
    error = ProviderError(
        "rate_limit",
        "busy",
        streamed not in {"text", "tool"},
        retry_after_s=3.0,
        partial=assistant("partial", stop_reason="error") if streamed == "partial" else None,
    )
    prefix = (
        [TextDelta(0, "x")] if streamed == "text" else [ToolCallStart(0, "a", "echo")] if streamed == "tool" else []
    )
    provider = ScriptedProvider([prefix + [error], [Done(assistant())]])
    agent = make_agent(provider)
    events = await collect(agent)
    if streamed in {"text", "tool"}:
        assert len(provider.requests) == 1
        assert events[-1].reason == "error"
        assert sleeps == []
    else:
        # A partial of a retried attempt is never context. The provider owns retryability.
        assert len(provider.requests) == agent.models.resolutions == 2
        assert sleeps == [3.0]
        assert events[-1].reason == "completed"


async def test_retry_keeps_a_usage_only_partial_out_of_the_context():
    # A usage-only partial is attempt data, never context: the retry resends the original request.
    store = InMemoryTranscriptStore()
    first_partial = AssistantMessage(
        content=(),
        origin=assistant().origin,
        stop_reason="error",
        usage=Usage(input_tokens=5, output_tokens=2, cache_write_tokens=1),
    )
    successful = AssistantMessage(
        content=assistant("done").content,
        origin=assistant().origin,
        stop_reason="stop",
        usage=Usage(input_tokens=7, output_tokens=3, cache_read_tokens=4),
    )
    provider = ScriptedProvider(
        [
            [ProviderError("rate_limit", "busy", True, partial=first_partial)],
            [Done(successful)],
        ]
    )

    events = await collect(
        make_agent(provider, store=store, retry=RetryPolicy(initial_delay_s=0))
    )

    assert events[-1].reason == "completed"
    assert provider.requests[1].messages == provider.requests[0].messages
    responses = [row.message for row in await store.load("session") if row.kind == "response"]
    assert [response.usage for response in responses] == [successful.usage]
    # Without context management too, the attempt's usage is exactly one non-context audit row.
    assert [attempt["usage"]["input_tokens"] for _, _, attempt in store.attempts] == [5]


class _FailingResponses(InMemoryTranscriptStore):
    async def append_response(self, session_id, message, *, final, request=None):
        raise RuntimeError("disk full")


def _billed(content, stop_reason="stop", input_tokens=9):
    return AssistantMessage(tuple(content), assistant().origin, stop_reason, usage=Usage(input_tokens=input_tokens))


_TWIN = (ToolCallBlock("same", "echo", {}), ToolCallBlock("same", "echo", {}))


@pytest.mark.parametrize(
    "scripts,context,store_type,audited,reason",
    [
        # Attempts that never became a response: each is exactly one audit row, whatever the exit.
        pytest.param([[ProviderError("rate_limit", "busy", True, partial=_billed((), "error"))], [Done(assistant("ok"))]], False, InMemoryTranscriptStore, 1, "completed", id="retried"),
        pytest.param([[ProviderError("overflow", "prompt is too long", False, partial=_billed((), "error"))]], True, InMemoryTranscriptStore, 1, "context_exhausted", id="relieved"),
        pytest.param([[ProviderError("server", "down", False, partial=_billed((), "error"))]], False, InMemoryTranscriptStore, 1, "error", id="usage-only terminal"),
        pytest.param([[Done(_billed(_TWIN, "tool_use"))]], False, InMemoryTranscriptStore, 1, "error", id="rejected response"),
        pytest.param([[ProviderError("server", "down", False, partial=_billed(_TWIN, "error"))]], False, InMemoryTranscriptStore, 1, "error", id="rejected partial"),
        pytest.param([[Done(_billed((text("ok"),)))]], False, _FailingResponses, 1, "error", id="commit failed"),
        # Attempts that became a response keep their usage in its row: no audit row.
        pytest.param([[Done(_billed((text("ok"),)))]], False, InMemoryTranscriptStore, 0, "completed", id="committed"),
        pytest.param([[ProviderError("server", "down", False, partial=_billed((text("half"),), "error"))]], False, InMemoryTranscriptStore, 0, "error", id="committed partial"),
    ],
)
async def test_the_ledger_audits_every_attempt_that_never_became_a_response(scripts, context, store_type, audited, reason):
    # Invariant 4: the ledger owns the attempt audit on every exit of the model call, not each exit path.
    store = store_type()
    options = {"context": ContextConfig()} if context else {}
    agent = make_agent(ScriptedProvider(scripts), store=store, retry=RetryPolicy(initial_delay_s=0), **options)
    events = await collect(agent)
    assert events[-1].reason == reason
    assert [attempt["usage"]["input_tokens"] for _, _, attempt in store.attempts] == [9] * audited
    committed = [row.message.usage for row in await store.load("session") if row.kind == "response"]
    assert len(committed) + audited == sum(1 for script in scripts if script)  # every attempt counted once


async def test_retry_usage_partial_never_enters_the_context_when_budget_expires(monkeypatch):
    real_sleep = asyncio.sleep

    async def slow_sleep(delay):
        del delay
        await real_sleep(0.01)

    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", slow_sleep)
    partial = AssistantMessage(
        content=(),
        origin=assistant().origin,
        stop_reason="error",
        usage=Usage(input_tokens=5),
    )
    provider = ScriptedProvider(
        [[ProviderError("rate_limit", "busy", True, partial=partial)], [Done(assistant("done"))]]
    )
    store = InMemoryTranscriptStore()
    agent = make_agent(
        provider,
        store=store,
        retry=RetryPolicy(initial_delay_s=0, max_elapsed_s=0.005),
    )

    events = await collect(agent)

    assert len(provider.requests) == 1
    assert not [row for row in await store.load("session") if row.kind == "response"]
    assert events[-1].reason == "error"


@pytest.mark.parametrize(
    "kind,retryable,reason,count",
    [
        ("server", True, "error", 3),
        ("unknown", True, "error", 3),
        ("auth", False, "error", 1),
        ("overflow", False, "context_exhausted", 1),
        ("aborted", False, "aborted", 1),
    ],
)
async def test_retry_budget_and_terminal_error_taxonomy(kind, retryable, reason, count):
    provider = ScriptedProvider([[ProviderError(kind, "clear error", retryable)]] * 4)
    agent = make_agent(provider, retry=RetryPolicy(initial_delay_s=0))
    events = await collect(agent)
    assert len(provider.requests) == count
    assert events[-1].reason == reason
    assert [(event.kind, event.message) for event in events if isinstance(event, AgentError)] == [(kind, "clear error")]


class _Failing(Hooks):
    def __init__(self, step: str) -> None:
        self.step = step

    async def before_model(self, request, ctx):
        if self.step == "before_model":
            raise RuntimeError("hook failed")

    async def after_run(self, outcome, ctx):
        if self.step == "after_run":
            raise RuntimeError("cleanup failed")


async def _cancelled(request, cancel):
    raise asyncio.CancelledError()
    yield  # an async generator


_TOOL_CALL = ToolCallBlock("call", "echo", {"x": 1})
#: Every place the loop emits ``AgentError``, each row a run that reaches it: (site, kind, origin, scripts, options).
#: ``source`` means the served model produced the failure, which an adapter may record against the route.
ERROR_SITES = {
    "provider error": ("provider", "auth", "source", [[ProviderError("auth", "denied", False)]], {}),
    # An overflow says our request was too large, not that the source failed.
    "provider overflow": ("provider", "overflow", "local", [[ProviderError("overflow", "too long", False)]], {}),
    "provider stream aborted by the loop": (
        "provider", "aborted", "local", [[ProviderError("aborted", "stop", False)]], {}
    ),
    "failed answer": ("failed answer", "error", "source", [[Done(assistant("half", stop_reason="error"))]], {}),
    "answer aborted by the loop": (
        "failed answer", "aborted", "local", [[Done(assistant("half", stop_reason="aborted"))]], {}
    ),
    "empty answer": ("empty answer", "empty_response", "source", [[Done(assistant(""))]], {}),
    "refusal": ("empty answer", "refusal", "source", [[Done(assistant("", stop_reason="refusal"))]], {}),
    "tool call cut by the output limit past the retries": (
        "length",
        "length",
        "source",
        [[Done(assistant(calls=[_TOOL_CALL], stop_reason="length"))]],
        {"retry": RetryPolicy(max_retries=0, initial_delay_s=0)},
    ),
    "tool calls under a stop": (
        "stop", "stop", "source", [[Done(assistant(calls=[_TOOL_CALL], stop_reason="stop"))]], {}
    ),
    "a response the transcript cannot hold": (
        "exception",
        "ProviderProtocolViolation",
        "source",
        [[Done(assistant(calls=[ToolCallBlock("same", "echo"), ToolCallBlock("same", "echo")]))]],
        {},
    ),
    "a hook failure": ("exception", "RuntimeError", "local", [], {"hooks": [_Failing("before_model")]}),
    # A request whose newest unit alone cannot fit: moving the conversation out would not help, and the kind says so.
    "an input that cannot fit": ("exhausted", "input_too_large", "local", [], {"context": ContextConfig()}),
    "a cancelled dependency": ("cancelled", "dependency_cancelled", "local", [_cancelled], {}),
    "a cleanup diagnostic": (
        "diagnostic", "RuntimeError", "local", [[Done(assistant("ok"))]], {"hooks": [_Failing("after_run")]}
    ),
}


@pytest.mark.parametrize("case", list(ERROR_SITES))
async def test_every_error_says_whether_the_served_source_produced_it(case):
    site, kind, origin, scripts, options = ERROR_SITES[case]
    agent = make_agent(ScriptedProvider(scripts), tools=[FakeTool()], **options)
    value = "x" * 160_000 if "context" in options else "hello"
    events = await collect(agent, value=value)
    errors = [event for event in events if isinstance(event, AgentError)]
    assert [(error.kind, error.origin) for error in errors] == [(kind, origin)]
    # The run names the error that decided its outcome; a diagnostic never does.
    assert events[-1].cause == (None if site == "diagnostic" else errors[0])


async def test_a_diagnostic_before_the_failure_is_never_the_cause():
    class Stream:
        def __init__(self, terminal, failing_close):
            self.terminal, self.failing_close = terminal, failing_close

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.terminal is None:
                raise StopAsyncIteration
            terminal, self.terminal = self.terminal, None
            return terminal

        async def aclose(self):
            if self.failing_close:
                raise OSError("stream close failed")

    class Provider:
        protocol = "anthropic"
        calls = 0

        def stream(self, request, cancel):
            self.calls += 1
            if self.calls == 1:  # the first round's stream fails to close: a diagnostic, flushed after the round
                return Stream(Done(assistant(calls=[ToolCallBlock("a", "echo")])), True)
            return Stream(ProviderError("server", "upstream failed", False), False)

    events = await collect(make_agent(Provider(), tools=[FakeTool()]))
    errors = [(event.kind, event.origin) for event in events if isinstance(event, AgentError)]
    assert errors == [("stream_cleanup", "local"), ("server", "source")]
    assert (events[-1].cause.kind, events[-1].cause.origin) == ("server", "source")


async def test_abort_closes_provider_stream_and_allows_a_new_turn():
    entered = asyncio.Event()

    async def blocked(request, cancel):
        entered.set()
        await asyncio.Event().wait()
        yield Done(assistant())

    provider = ScriptedProvider([blocked, [Done(assistant("new"))]])
    agent = make_agent(provider)
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    agent.abort("stop")
    events = await asyncio.wait_for(run, 1)
    assert events[-1].reason == "aborted"
    assert provider.closed_streams == 1
    assert [row.kind for row in await agent.store.load("session")] == ["input"]
    next_events = await collect(agent, "next", turn_id="next-turn")
    assert next_events[0].seq == 0
    assert next_events[-1].reason == "completed"


async def test_abort_kills_foreground_through_JobHost_but_preserves_handed_over_jobs():
    host = FakeJobHost()
    entered = asyncio.Event()
    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("bg", "bash"), ToolCallBlock("fg", "bash")]))]])
    agent = make_agent(provider, jobs=host)

    async def execute(arguments, ctx):
        job = await agent.jobs.start(
            "test", cwd=ctx.cwd, env=ctx.env, timeout_s=None, session_id=ctx.session_id, tool_call_id=ctx.tool_call_id
        )
        if ctx.tool_call_id == "bg":
            watch = await agent.jobs.hand_over(job)
            return ToolResult((text("watch"),), details={"watch_id": watch, "job_id": job})
        entered.set()
        await agent.jobs.wait(job, deadline_s=None)
        return ToolResult((text("foreground finished"),))

    agent.set_tools([FakeTool("bash", execute=execute)])
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    agent.abort()
    events = await asyncio.wait_for(run, 1)
    assert events[-1].reason == "aborted"
    assert host.killed == ["job_2"]
    assert agent.jobs.stop_reason("job_2") == "aborted"  # bash reports the end from the recorded reason
    assert host.status("job_1").state == "running"
    assert [row.message.tool_call_id for row in await agent.store.load("session") if row.kind == "tool_result"] == [
        "bg"
    ]


@pytest.mark.parametrize("transition", ["start", "hand_over"])
async def test_abort_during_job_ownership_transition_waits_then_kills_only_foreground(transition):
    entered, release = asyncio.Event(), asyncio.Event()

    class Host(FakeJobHost):
        async def start(self, *args, **kwargs):
            job = await super().start(*args, **kwargs)
            if transition == "start":
                entered.set()
                await release.wait()
            return job

        async def hand_over(self, job_id):
            watch = await super().hand_over(job_id)
            entered.set()
            await release.wait()
            return watch

    host = Host()
    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("a", "bash")]))]])
    agent = make_agent(provider, jobs=host)

    async def execute(arguments, ctx):
        job = await agent.jobs.start(
            "test", cwd=ctx.cwd, env=ctx.env, timeout_s=None, session_id=ctx.session_id, tool_call_id=ctx.tool_call_id
        )
        if transition == "hand_over":
            await agent.jobs.hand_over(job)
        await asyncio.Event().wait()

    agent.set_tools([FakeTool("bash", execute=execute)])
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    agent.abort()
    release.set()
    events = await asyncio.wait_for(run, 1)
    assert events[-1].reason == "aborted"
    assert len(host.starts) == 1
    assert host.killed == (["job_1"] if transition == "start" else [])


async def test_abort_interrupts_retry_backoff_without_an_extra_provider_request(monkeypatch):
    entered = asyncio.Event()

    async def backoff(delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", backoff)
    provider = ScriptedProvider([[ProviderError("server", "temporary", True)]])
    agent = make_agent(provider)
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    agent.abort()
    assert (await asyncio.wait_for(run, 1))[-1].reason == "aborted"
    assert len(provider.requests) == 1


async def test_closing_event_consumer_cancels_work_and_releases_run_ownership():
    provider = ScriptedProvider([[Done(assistant())]])
    agent = make_agent(provider)
    stream = agent.run(input_row("input", "hello"), turn_id="turn")
    assert isinstance(await anext(stream), RunStarted)
    await stream.aclose()
    assert await agent.steer(input_row("late", "no")) is False


async def test_concurrent_run_is_refused_without_interrupting_the_owner():
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(request, cancel):
        entered.set()
        await release.wait()
        yield Done(assistant())

    agent = make_agent(ScriptedProvider([blocked]))
    run = asyncio.create_task(collect(agent))
    await entered.wait()
    with pytest.raises(RuntimeError, match="active run"):
        await collect(agent, "other", turn_id="other")
    release.set()
    assert (await run)[-1].reason == "completed"


@pytest.mark.parametrize("capability", [None, False, True])
async def test_resolved_nullable_capabilities_gate_each_request(capability):
    provider = ScriptedProvider([[Done(assistant())]])
    agent = make_agent(provider, tools=[FakeTool()], reasoning_effort="high", max_tokens=2000)
    agent.models = FakeModelRouter(
        provider,
        [
            ModelSelection(
                ENDPOINT,
                ModelCapabilities(
                    supports_tools=None,
                    supports_images=capability,
                    supports_reasoning=capability,
                    max_output_tokens=1000 if capability is True else None,
                    reasoning_efforts=("low", "high"),
                ),
            )
        ],
    )
    await collect(agent)
    request = provider.requests[0]
    assert request.supports_images is (capability is True)
    assert [spec.name for spec in request.tools] == ["echo"]
    assert request.reasoning_effort == ("high" if capability is True else None)
    assert request.max_tokens == (1000 if capability is True else 2000)


@pytest.mark.parametrize("initial", [True, False])
async def test_tool_incapable_route_is_refused_before_execution_or_any_request(initial):
    hooks = []

    class RecordHook(Hooks):
        async def before_run(self, input, ctx):
            hooks.append("before_run")

    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("a", "echo")]))]])
    tool = FakeTool()
    agent = make_agent(provider, tools=[tool], hooks=[RecordHook()])
    denied = ModelSelection(ENDPOINT, ModelCapabilities(supports_tools=False))
    agent.models = FakeModelRouter(
        provider,
        [denied] if initial else [ModelSelection(ENDPOINT, ModelCapabilities(supports_tools=True)), denied],
    )
    events = await collect(agent)
    assert events[-1].reason == "error"
    assert [(event.kind, event.message) for event in events if isinstance(event, AgentError)] == [
        ("UnsupportedModelRoute", "The selected model does not support tools; the agent requires tool support.")
    ]
    assert len(provider.requests) == (0 if initial else 1)
    if initial:
        assert hooks == tool.calls == []
        assert await agent.store.load("session") == []
    else:
        assert len(tool.calls) == 1


@pytest.mark.parametrize(
    "configured,maximum,expected",
    [(None, None, 8192), (20000, None, 8192), (1000, None, 1000), (20000, 16000, 16000)],
)
async def test_output_budget_uses_configured_budget_and_known_or_default_provider_limit(configured, maximum, expected):
    provider = ScriptedProvider([[Done(assistant())]])
    agent = make_agent(provider, **({"max_tokens": configured} if configured is not None else {}))
    agent.models = FakeModelRouter(provider, [ModelSelection(ENDPOINT, ModelCapabilities(max_output_tokens=maximum))])
    await collect(agent)
    assert provider.requests[0].max_tokens == expected


@pytest.mark.parametrize("window,expected", [(None, 128000), (32000, 32000)])
def test_the_context_budget_defaults_an_unknown_window_without_forging_capabilities(window, expected):
    selection = ModelSelection(ENDPOINT, ModelCapabilities(context_window=window))
    request = ModelRequest(ENDPOINT, "", (), (), 8192)
    assert budget(request, selection.capabilities, transcript=()).window == expected
    assert selection.capabilities.context_window == window


async def test_reasoning_effort_must_be_declared_by_the_route():
    provider = ScriptedProvider([[Done(assistant())]])
    agent = make_agent(provider, reasoning_effort="high")
    agent.models = FakeModelRouter(
        provider,
        [
            ModelSelection(
                ENDPOINT,
                ModelCapabilities(supports_reasoning=True, reasoning_efforts=("low",)),
            )
        ],
    )
    await collect(agent)
    assert provider.requests[0].reasoning_effort is None


@pytest.mark.parametrize("stop_reason", ["stop", "length", "refusal", "safety"])
@pytest.mark.parametrize("shape", ["empty", "whitespace", "thinking", "visible"])
@pytest.mark.parametrize("continuation", ["final", "steer", "follow_up"])
async def test_tool_free_final_reply_is_committed_and_empty_success_is_an_error(stop_reason, shape, continuation):
    # The old refusal/safety-only table missed empty successful finals.
    # Pending-input responses are non-final and must still be allowed to continue.
    content = {
        "empty": (),
        "whitespace": (text(" \n"),),
        "thinking": (ThinkingBlock("private reasoning"),),
        "visible": (ThinkingBlock("private reasoning"), text("visible reply")),
    }[shape]
    message = replace(assistant(stop_reason=stop_reason), content=content)

    async def first(request, cancel):
        if continuation != "final":
            assert await getattr(agent, continuation)(input_row("pending", "continue"))
        yield Done(message)

    provider = ScriptedProvider([first, [Done(assistant("next visible reply"))]])
    agent = make_agent(provider)
    events = await collect(agent)
    rows = await agent.store.load("session")
    responses = [row for row in rows if row.kind == "response"]
    assert responses[0].message == message
    committed = [event for event in events if isinstance(event, MessageCommitted)]
    expected_finals = [True] if continuation == "final" else [False, True]
    assert [agent.store.final[row.row_id] for row in responses] == expected_finals
    assert [event.final for event in committed] == expected_finals
    assert [event.message_id for event in committed] == [row.row_id for row in responses]
    assert len(provider.requests) == provider.closed_streams == len(expected_finals)
    assert [row.row_id for row in rows if row.kind == "input"] == (
        ["input"] if continuation == "final" else ["input", "pending"]
    )
    errors = [event for event in events if isinstance(event, AgentError)]
    if shape == "visible" or continuation != "final":
        assert errors == []
        assert events[-1].reason == "completed"
    else:
        assert len(errors) == 1
        assert errors[0].kind == (stop_reason if stop_reason in {"refusal", "safety"} else "empty_response")
        assert errors[0].message
        assert committed[0].seq < errors[0].seq < events[-1].seq
        assert events[-1].reason == "error"
    project(rows)


@pytest.mark.parametrize("source", ["provider", "tool", "before_model", "after_run"])
async def test_dependency_cancelled_error_emits_a_terminal_event_and_preserves_consumer(source):
    class CancelHook(Hooks):
        async def before_model(self, request, ctx):
            if source == "before_model":
                raise asyncio.CancelledError()

        async def after_run(self, outcome, ctx):
            if source == "after_run":
                raise asyncio.CancelledError()

    async def stream(request, cancel):
        if source == "provider":
            raise asyncio.CancelledError()
        yield Done(assistant(calls=[ToolCallBlock("a", "echo")] if source == "tool" else []))

    async def execute(arguments, ctx):
        raise asyncio.CancelledError()

    agent = make_agent(ScriptedProvider([stream]), hooks=[CancelHook()], tools=[FakeTool(execute=execute)])
    events = await collect(agent)
    assert isinstance(events[-1], RunEnded)
    assert events[-1].reason == ("completed" if source == "after_run" else "aborted")
    assert any(isinstance(event, AgentError) and event.kind == "dependency_cancelled" for event in events)


@pytest.mark.parametrize("step", ["response", "tool_result"])
async def test_every_committed_output_is_announced_even_when_hook_state_cannot_be_saved(step):
    class FailingStore(InMemoryTranscriptStore):
        async def append_payload(self, session_id, kind, payload):
            raise OSError("state persistence unavailable")

    class DirtyState(Hooks):
        async def before_model(self, request, ctx):
            if step == "response":
                ctx.state["dirty"] = True

        async def after_tool(self, call, result, ctx):
            if step == "tool_result":
                ctx.state["dirty"] = True

    store = FailingStore()
    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("a", "echo")]))]])
    agent = make_agent(provider, store=store, hooks=[DirtyState()], tools=[FakeTool()])
    events = await collect(agent)
    committed = [row for row in await store.load("session") if row.kind in {"response", "tool_result"}]
    announced = [
        event.message_id if isinstance(event, MessageCommitted) else event.event_id
        for event in events
        if isinstance(event, (MessageCommitted, ToolFinished))
    ]
    assert [row.row_id for row in committed] == announced
    assert events[-1].reason == "error"


@pytest.mark.parametrize("termination", ["terminate", "hook", "abort", "provider_error"])
async def test_early_termination_returns_accepted_unconsumed_inputs_to_adapter(termination):
    class StopHook(Hooks):
        async def after_tool(self, call, result, ctx):
            if termination == "hook":
                return End()

    async def accept():
        assert await agent.steer(input_row("steer", "steer"))
        assert await agent.follow_up(input_row("follow", "follow"))

    async def stream(request, cancel):
        if termination == "provider_error":
            await accept()
            yield ProviderError("auth", "denied", False)
        else:
            yield Done(assistant(calls=[ToolCallBlock("a", "echo")]))

    async def execute(arguments, ctx):
        await accept()
        if termination == "abort":
            agent.abort()
            await ctx.cancel.wait()
        return ToolResult((text("done"),), terminate=termination == "terminate")

    agent = make_agent(ScriptedProvider([stream]), tools=[FakeTool(execute=execute)], hooks=[StopHook()])
    await collect(agent)
    with pytest.raises(RuntimeError, match="unconsumed"):
        await collect(agent, "new")
    pending = await agent.take_pending_inputs()
    assert [item.message_id for item in pending] == ["steer", "follow"]
    assert await agent.take_pending_inputs() == ()
    assert [row.row_id for row in await agent.store.load("session") if row.kind == "input"] == ["input"]


async def test_failed_queued_input_commit_keeps_pending_input_and_announces_prior_commits():
    class Store(InMemoryTranscriptStore):
        async def consume_input(self, session_id, message_id, message):
            if message_id == "second":
                raise OSError("input unavailable")
            return await super().consume_input(session_id, message_id, message)

    async def stream(request, cancel):
        assert await agent.steer(input_row("first", "first"))
        assert await agent.steer(input_row("second", "second"))
        yield Done(assistant())

    store = Store()
    agent = make_agent(ScriptedProvider([stream]), store=store)
    events = await collect(agent)
    durable_response = next(row for row in await store.load("session") if row.kind == "response")
    assert [event.message_id for event in events if isinstance(event, MessageCommitted)] == [durable_response.row_id]
    assert [event.message_id for event in events if isinstance(event, SteerApplied)] == ["first"]
    assert [item.message_id for item in await agent.take_pending_inputs()] == ["second"]


async def test_partial_response_commit_failure_still_emits_a_terminal_error():
    class Store(InMemoryTranscriptStore):
        async def append_response(self, session_id, message, *, final):
            raise OSError("response persistence unavailable")

    provider = ScriptedProvider(
        [
            [
                ProviderError("server", "stream failed", False, partial=assistant("partial", stop_reason="error")),
            ]
        ]
    )
    events = await collect(make_agent(provider, store=Store()))
    assert events[-1].reason == "error"
    assert any(
        isinstance(event, AgentError) and event.message == "response persistence unavailable" for event in events
    )


@pytest.mark.parametrize(
    "phase,reason",
    [
        ("before_start", "aborted"),
        ("before_store", "aborted"),
        ("model", "aborted"),
        ("tool", "aborted"),
        ("dependency", "aborted"),
        ("consumer", "aborted"),
        ("tool_exception", "completed"),
        ("tool_overflow", "completed"),
        ("completed", "completed"),
        ("cleanup", "completed"),
    ],
)
async def test_run_lifecycle_owns_admission_and_releases_every_foreground_job(phase, reason):
    """A single lifecycle table covers admission, cancellation and release.

    Earlier tests checked cancellation outcomes individually but missed work
    admitted after abort and jobs abandoned on non-abort execution exits.
    """
    entered, release = asyncio.Event(), asyncio.Event()
    host = FakeJobHost()
    hook_calls, outcomes, events, store_writes = [], [], [], []

    class Store(InMemoryTranscriptStore):
        async def load(self, session_id):
            if phase == "before_start":
                entered.set()
                await release.wait()
            return await super().load(session_id)

        async def consume_input(self, *args):
            store_writes.append("input")
            return await super().consume_input(*args)

    class Observe(Hooks):
        async def before_run(self, input, ctx):
            hook_calls.append("before_run")
            if phase == "before_store":
                ctx.cancel.cancel("abort before storage")

        async def after_run(self, outcome, ctx):
            outcomes.append(outcome.reason)
            if phase == "cleanup":
                await agent.jobs.start(
                    "cleanup",
                    cwd="/test-owned",
                    env={},
                    timeout_s=None,
                    session_id=ctx.session_id,
                    tool_call_id="cleanup",
                )

    async def stream(request, cancel):
        if phase == "model":
            entered.set()
            await release.wait()
        yield Done(assistant(calls=[ToolCallBlock("a", "bash")]))

    async def execute(arguments, ctx):
        watched = await agent.jobs.start(
            "watched",
            cwd=ctx.cwd,
            env=ctx.env,
            timeout_s=None,
            session_id=ctx.session_id,
            tool_call_id=ctx.tool_call_id,
        )
        await agent.jobs.hand_over(watched)
        await agent.jobs.start(
            "foreground",
            cwd=ctx.cwd,
            env=ctx.env,
            timeout_s=None,
            session_id=ctx.session_id,
            tool_call_id=ctx.tool_call_id,
        )
        if phase == "dependency":
            raise asyncio.CancelledError()
        if phase == "tool_exception":
            raise ValueError("output parsing failed")
        if phase == "tool_overflow":
            raise OverflowError("tool argument too large")
        if phase in {"tool", "consumer"}:
            entered.set()
            ctx.on_progress("working")
            await release.wait()
        return ToolResult((text("done"),))

    provider = ScriptedProvider([stream, [Done(assistant())]])
    agent = make_agent(provider, store=Store(), hooks=[Observe()], tools=[FakeTool("bash", execute=execute)], jobs=host)
    iterator = agent.run(input_row("input", "hello"), turn_id="turn")

    async def consume():
        try:
            async for event in iterator:
                events.append(event)
                if phase == "consumer" and isinstance(event, ToolProgress):
                    break
        finally:
            await iterator.aclose()

    run = asyncio.create_task(consume())
    if phase in {"before_start", "model", "tool"}:
        await asyncio.wait_for(entered.wait(), 1)
        agent.abort("test cancellation")
        release.set()
    await asyncio.wait_for(run, 1)
    if phase == "consumer":
        assert not any(isinstance(event, RunEnded) for event in events)
        assert outcomes == [reason]
    else:
        assert isinstance(events[-1], RunEnded)
        assert events[-1].reason == reason
    if phase == "before_start":
        assert hook_calls == []
    if phase in {"before_start", "before_store"}:
        assert provider.requests == []
        assert store_writes == []
    for job_id in host.states:
        assert host.status(job_id).state == ("running" if job_id in host.watches else "gone")
    assert not set(host.killed) & host.watches.keys()
    assert await agent.steer(input_row("late", "late")) is False


@pytest.mark.parametrize("bad_state", [{1: "value"}, {"value": ("item",)}, {"value": float("nan")}, {"value": {1}}])
async def test_hook_state_rejects_non_json_shapes_without_coercion(bad_state):
    class Invalid(Hooks):
        async def before_run(self, input, ctx):
            ctx.state = bad_state

    agent = make_agent(ScriptedProvider([[Done(assistant())]]), hooks=[Invalid()])
    events = await collect(agent)
    assert events[-1].reason == "error"
    assert any(isinstance(event, AgentError) and event.kind == "HookStateError" for event in events)
    assert await agent.store.load("session") == []
    assert agent.snapshot().state == {}


async def test_hook_state_representation_distinguishes_bool_int_and_survives_resume():
    seen = []

    class Update(Hooks):
        async def before_run(self, input, ctx):
            ctx.state["value"] = 1

        async def after_model(self, message, ctx):
            ctx.state["value"] = True

        async def after_run(self, outcome, ctx):
            seen.append(ctx.state.copy())

    agent = make_agent(ScriptedProvider([[Done(assistant())]]), hooks=[Update()])
    await collect(agent)
    assert type(agent.snapshot().state["value"]) is bool
    rows = await agent.store.load("session")
    states = [row.payload["state"] for row in rows if row.kind == "agent_state"]
    assert len(states) == 2
    assert type(states[0]["value"]) is int
    assert type(states[1]["value"]) is bool

    class Resume(Hooks):
        async def before_run(self, input, ctx):
            seen.append(ctx.state.copy())

    resumed = make_agent(ScriptedProvider([[Done(assistant())]]), store=agent.store, hooks=[Resume()])
    await collect(resumed, "next")
    assert all(type(state["value"]) is bool for state in seen)
    assert len([row for row in await agent.store.load("session") if row.kind == "agent_state"]) == 2


async def test_invalid_cleanup_hook_state_is_diagnostic_without_overwriting_abort():
    class InvalidCleanup(Hooks):
        async def before_model(self, request, ctx):
            ctx.cancel.cancel()

        async def after_run(self, outcome, ctx):
            ctx.state["invalid"] = ("tuple",)

    agent = make_agent(ScriptedProvider([]), hooks=[InvalidCleanup()])
    events = await collect(agent)
    assert events[-1].reason == "aborted"
    assert any(isinstance(event, AgentError) and event.kind == "HookStateError" for event in events)
    assert agent.snapshot().state == {}


async def test_request_headers_are_detached_from_router_selection_across_turns():
    class Rewrite(Hooks):
        first = True

        async def before_model(self, request, ctx):
            if self.first:
                request.endpoint.request_headers["X-Transient"] = "only this request"
                self.first = False

    endpoint = replace(ENDPOINT, request_headers={"X-Base": "stable"})
    selection = ModelSelection(endpoint, ModelCapabilities())
    provider = ScriptedProvider([[Done(assistant())], [Done(assistant())]])
    agent = make_agent(provider, hooks=[Rewrite()])
    agent.models = FakeModelRouter(provider, [selection])
    await collect(agent)
    await collect(agent, "next")
    assert provider.requests[0].endpoint.request_headers == {"X-Base": "stable", "X-Transient": "only this request"}
    assert provider.requests[1].endpoint.request_headers == {"X-Base": "stable"}
    assert endpoint.request_headers == {"X-Base": "stable"}


@pytest.mark.parametrize("cause", ["tool_error", "dependency", "abort"])
async def test_failed_foreground_kill_is_reported_and_does_not_skip_other_handles(cause):
    class Host(FakeJobHost):
        def __init__(self):
            super().__init__()
            self.attempts = []

        async def kill(self, job_id, *, reason="killed"):
            self.attempts.append(job_id)
            if job_id == "job_2":
                raise OSError("kill denied")
            await super().kill(job_id, reason=reason)

    host = Host()
    provider = ScriptedProvider([[Done(assistant(calls=[ToolCallBlock("a", "bash")]))], [Done(assistant())]])
    agent = make_agent(provider, jobs=host)

    async def execute(arguments, ctx):
        for index in range(3):
            job = await agent.jobs.start(
                "fake",
                cwd=ctx.cwd,
                env=ctx.env,
                timeout_s=None,
                session_id=ctx.session_id,
                tool_call_id=ctx.tool_call_id,
            )
            if index == 0:
                await agent.jobs.hand_over(job)
        if cause == "dependency":
            raise asyncio.CancelledError()
        if cause == "abort":
            agent.abort()
            await asyncio.Event().wait()
        raise ValueError("tool failed")

    agent.set_tools([FakeTool("bash", execute=execute)])
    events = await collect(agent)
    assert events[-1].reason == ("error" if cause == "tool_error" else "aborted")
    assert any(
        isinstance(event, AgentError) and event.kind == "ForegroundCleanupError" and "kill denied" in event.message
        for event in events
    )
    assert host.attempts == ["job_2", "job_3", "job_2"]
    assert host.status("job_1").state == host.status("job_2").state == "running"
    assert host.status("job_3").state == "gone"
    assert len(provider.requests) == (2 if cause == "tool_error" else 1)
    if cause == "dependency":
        errors = [event.kind for event in events if isinstance(event, AgentError)]
        assert errors[0] == "dependency_cancelled"


@pytest.mark.parametrize("phase", ["abort", "consumer"])
async def test_admitted_commit_finishes_before_cancellation_releases_run_ownership(phase):
    entered, release = asyncio.Event(), asyncio.Event()
    interrupted = []

    class Store(InMemoryTranscriptStore):
        async def append_response(self, *args, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(True)
                raise
            return await super().append_response(*args, **kwargs)

    agent = make_agent(ScriptedProvider([[Done(assistant())]]), store=Store())
    task = asyncio.create_task(collect(agent))
    await entered.wait()
    if phase == "abort":
        agent.abort()
    else:
        task.cancel()
        # Let generator closure reach its worker; no wall-clock timing.
        for _ in range(3):
            await asyncio.sleep(0)
    release.set()
    if phase == "consumer":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        events = await task
        assert events[-1].reason == "aborted"
        assert any(isinstance(event, MessageCommitted) for event in events)
    assert interrupted == []
    rows = await agent.store.load("session")
    assert rows[-1].kind == "response"
    assert agent.snapshot().context_seq == rows[-1].context_seq
    assert await agent.steer(input_row("late", "late")) is False
