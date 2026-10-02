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
from core.agent_core.ai.provider import Done, ProviderError, TextDelta, ThinkingDelta, ToolCallStart
from core.agent_core.harness.projection import project
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, UserMessage, text
from core.agent_core.tools.base import ToolResult
from tests.agent_core.fakes import (
    FakeJobHost,
    FakeModelRouter,
    FakeTool,
    InMemoryTranscriptStore,
    ScriptedProvider,
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


async def collect(agent, row_id="input", value="hello", run_id="turn"):
    return [event async for event in agent.run(input_row(row_id, value), run_id=run_id)]


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
            ctx.state["finished"] = outcome.run_id

    provider = ScriptedProvider([[Done(assistant())], [Done(assistant())]])
    agent = make_agent(provider, hooks=[Setup()], tools=[FakeTool("base")], system="base system")
    await collect(agent, "first", run_id="first-turn")
    assert agent.snapshot().state == {"finished": "first-turn"}
    await collect(agent, "second", run_id="second-turn")
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
    async for event in agent.run(input_row("input", "hello"), run_id="the-turn-id"):
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
    assert {event.run_id for event in events} == {"the-turn-id"}
    assert len(next(event for event in events if isinstance(event, ToolStarted)).preview) <= 500


@pytest.mark.parametrize("streamed", [False, "text", "tool", "partial"])
async def test_retry_only_before_output_and_re_resolves_the_route(streamed, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", sleep)
    error = ProviderError(
        "rate_limit",
        "busy",
        True,
        retry_after_s=3.0,
        partial=assistant("partial", stop_reason="error") if streamed == "partial" else None,
    )
    prefix = (
        [TextDelta(0, "x")] if streamed == "text" else [ToolCallStart(0, "a", "echo")] if streamed == "tool" else []
    )
    provider = ScriptedProvider([prefix + [error], [Done(assistant())]])
    agent = make_agent(provider)
    events = await collect(agent)
    if streamed:
        assert len(provider.requests) == 1
        assert events[-1].reason == "error"
        assert sleeps == []
    else:
        assert len(provider.requests) == agent.models.resolutions == 2
        assert sleeps == [3.0]
        assert events[-1].reason == "completed"


@pytest.mark.parametrize(
    "kind,retryable,reason,count",
    [
        ("server", True, "error", 3),
        ("unknown", True, "error", 1),
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
    next_events = await collect(agent, "next", run_id="next-turn")
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
    stream = agent.run(input_row("input", "hello"), run_id="turn")
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
        await collect(agent, "other", run_id="other")
    release.set()
    assert (await run)[-1].reason == "completed"
