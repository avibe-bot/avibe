"""Primary outcomes survive independent cleanup; bad output stays uncommitted.

Previous lifecycle tests covered admission/release, not the primary x cleanup
cross product or the gap between receiving a terminal and committing it.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.agent_core.agent.events import AgentError, MessageCommitted, RunEnded, ToolFinished
from core.agent_core.agent.hooks import AgentInput, AlterResult, End, Hooks
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import RetryPolicy
from core.agent_core.ai.provider import Done, ProviderError
from core.agent_core.harness.projection import project
from core.agent_core.messages import LargeRef, TextBlock, ToolCallBlock, UserMessage, text
from core.agent_core.tools.base import ToolResult
from tests.agent_core.fakes import (
    FakeJobHost,
    FakeModelRouter,
    FakeTool,
    InMemoryTranscriptStore,
    ScriptedProvider,
    assistant,
    input_row,
)


def agent_for(provider, *, hooks=(), store=None, tools=()):
    return Agent(
        session_id="session",
        models=FakeModelRouter(provider),
        tools=tools,
        hooks=hooks,
        store=store or InMemoryTranscriptStore(),
        jobs=FakeJobHost(),
        cwd="/test-owned",
    )


async def collect(agent, row="input"):
    return [event async for event in agent.run(input_row(row, "hello"), turn_id=row)]


@pytest.mark.parametrize(
    "primary,reason,error_kind",
    [
        ("done", "completed", None),
        ("done_tools", "completed", None),
        ("tool_value", "completed", None),
        ("tool_overflow", "completed", None),
        ("tool_runtime", "completed", None),
        ("provider_error", "error", "auth"),
        ("partial", "error", "server"),
        ("overflow", "context_exhausted", "overflow"),
        ("abort", "aborted", None),
        ("hook_end", "ended_by_hook", None),
        ("refusal", "error", "refusal"),
        ("safety", "error", "safety"),
    ],
)
@pytest.mark.parametrize("cleanup", ["clean", "aclose", "after_run", "cancelled", "aclose_cancelled", "kill"])
async def test_primary_outcome_cross_cleanup_keeps_reason_rows_and_event_order(
    primary, reason, error_kind, cleanup, caplog
):
    store = InMemoryTranscriptStore()
    closed_rows, hook_outcomes = [], []
    has_tools = primary == "done_tools" or primary.startswith("tool_")

    class Observe(Hooks):
        async def after_model(self, message, ctx):
            if primary == "hook_end":
                return End()

        async def after_run(self, outcome, ctx):
            hook_outcomes.append(outcome.reason)
            if cleanup == "kill":
                await agent.jobs.start(
                    "test-owned",
                    cwd="/test-owned",
                    env={},
                    timeout_s=None,
                    session_id=ctx.session_id,
                    tool_call_id="cleanup",
                )
            if cleanup == "after_run":
                raise RuntimeError("after_run failed")
            if cleanup == "cancelled":
                raise asyncio.CancelledError()

    class Stream:
        def __init__(self, terminal):
            self.terminal = terminal

        def __aiter__(self):
            return self

        async def __anext__(self):
            if primary == "abort":
                agent.abort("user abort")
                await asyncio.Event().wait()
            if self.terminal is None:
                raise StopAsyncIteration
            terminal, self.terminal = self.terminal, None
            return terminal

        async def aclose(self):
            closed_rows.append([row.kind for row in await store.load("session")])
            if cleanup == "aclose":
                raise OSError("stream close failed")
            if cleanup == "aclose_cancelled":
                raise asyncio.CancelledError()

    class Provider:
        protocol = "anthropic"
        calls = 0

        def stream(self, request, cancel):
            self.calls += 1
            terminal = Done(assistant())
            if has_tools and self.calls == 1:
                terminal = Done(assistant(calls=[ToolCallBlock("a", "echo")]))
            elif primary == "provider_error":
                terminal = ProviderError("auth", "primary error", False)
            elif primary == "partial":
                terminal = ProviderError(
                    "server", "primary error", False, partial=assistant("partial", stop_reason="error")
                )
            elif primary == "overflow":
                terminal = ProviderError("overflow", "primary error", False)
            elif primary in {"refusal", "safety"}:
                terminal = Done(assistant("", stop_reason=primary))
            return Stream(terminal)

    async def execute(arguments, ctx):
        errors = {"tool_value": ValueError, "tool_overflow": OverflowError, "tool_runtime": RuntimeError}
        raise errors[primary]("tool failed: " + "x" * 1000)

    provider, tool = Provider(), FakeTool(execute=execute if primary.startswith("tool_") else None)
    agent = agent_for(provider, store=store, hooks=[Observe()], tools=[tool])

    class UnkillableHost(FakeJobHost):
        async def kill(self, job_id):
            raise OSError("kill failed")

    if cleanup == "kill":
        agent.jobs.host = UnkillableHost()
    events = await collect(agent)
    assert isinstance(events[-1], RunEnded)
    assert events[-1].reason == ("error" if cleanup == "kill" and reason == "completed" else reason)
    assert hook_outcomes == [reason]
    assert [event.seq for event in events] == list(range(len(events)))
    rows = await store.load("session")
    expected_rows = ["input"]
    if primary not in {"abort", "provider_error", "overflow"}:
        expected_rows.append("response")
    if has_tools:
        expected_rows.extend(["tool_result", "response"])
    assert [row.kind for row in rows] == expected_rows
    expected_closes = [["input", "response"], expected_rows] if has_tools else [expected_rows]
    assert closed_rows == expected_closes  # Received terminal is committed BEFORE aclose.
    committed = [event for event in events if isinstance(event, MessageCommitted)]
    responses = [row for row in rows if row.kind == "response"]
    assert [event.message_id for event in committed] == [row.row_id for row in responses]
    assert [event.final for event in committed] == (
        [False, True] if has_tools else [primary != "partial"] if responses else []
    )
    assert len(tool.calls) == (1 if has_tools else 0)
    assert provider.calls == (2 if has_tools else 1)
    if primary.startswith("tool_"):
        result = next(row.message for row in rows if row.kind == "tool_result")
        assert result.is_error
        assert result.content[0].text.startswith("tool failed: ")
        assert len(result.content[0].text) <= 500
        assert any(record.exc_info is not None for record in caplog.records)
    errors = [event for event in events if isinstance(event, AgentError)]
    expected_kinds = [error_kind] if error_kind else []
    if cleanup in {"aclose", "aclose_cancelled"}:
        expected_kinds.extend(["stream_cleanup"] * provider.calls)
    elif cleanup == "after_run":
        expected_kinds.append("RuntimeError")
    elif cleanup == "cancelled":
        expected_kinds.append("dependency_cancelled")
    elif cleanup == "kill":
        expected_kinds.append("ForegroundCleanupError")
    assert [event.kind for event in errors] == expected_kinds
    if has_tools and cleanup in {"aclose", "aclose_cancelled"}:
        assert committed[0].seq < errors[0].seq < next(event.seq for event in events if isinstance(event, ToolFinished))
    project(rows)


@pytest.mark.parametrize(
    "stage,fault",
    [
        (stage, fault)
        for stage in ("before_model", "after_model_text", "after_model_tools", "before_tool", "after_tool")
        for fault in ("clean", "state_fail", "result_fail", "abort_before", "abort_during", "abort_after")
        if fault != "result_fail" or stage in {"after_model_tools", "before_tool", "after_tool"}
    ],
)
async def test_hook_end_is_a_directive_until_required_commits_succeed(stage, fault):
    """Required commit failures precede outcome selection, unlike cleanup.

    The primary/cleanup table above cannot reach this gap: its End hook does
    not dirty state or require tool results after requesting termination.
    """
    has_tools = stage in {"after_model_tools", "before_tool", "after_tool"}
    hook_outcomes = []

    class Store(InMemoryTranscriptStore):
        async def append_payload(self, session_id, kind, payload):
            if fault == "state_fail":
                raise OSError("required state write failed")
            if not has_tools and fault == "abort_during":
                agent.abort("abort during state commit")
            row = await super().append_payload(session_id, kind, payload)
            if not has_tools and fault == "abort_after":
                agent.abort("abort after state commit")
            return row

        async def append_tool_result(self, session_id, message, *, details):
            if fault == "result_fail":
                raise OSError("required tool-result write failed")
            if fault == "abort_during":
                agent.abort("abort during result commit")
            row = await super().append_tool_result(session_id, message, details=details)
            if fault == "abort_after":
                agent.abort("abort after result commit")
            return row

    class Hook(Hooks):
        def end(self, ctx):
            ctx.state["ended_at"] = stage
            if fault == "abort_before":
                agent.abort("abort before required commit")
            return End()

        async def before_model(self, request, ctx):
            if stage == "before_model":
                return self.end(ctx)

        async def after_model(self, message, ctx):
            if stage in {"after_model_text", "after_model_tools"}:
                return self.end(ctx)

        async def before_tool(self, call, ctx):
            if stage == "before_tool":
                return self.end(ctx)

        async def after_tool(self, call, result, ctx):
            if stage == "after_tool":
                return self.end(ctx)

        async def after_run(self, outcome, ctx):
            hook_outcomes.append(outcome.reason)

    calls = [ToolCallBlock("a", "echo"), ToolCallBlock("b", "echo")] if has_tools else []
    provider = ScriptedProvider([[Done(assistant(calls=calls))]])
    tool = FakeTool()
    agent = agent_for(provider, hooks=[Hook()], store=Store(), tools=[tool])
    events = await collect(agent)
    reason = "ended_by_hook" if fault == "clean" else "aborted" if fault.startswith("abort_") else "error"
    assert events[-1].reason == reason
    assert hook_outcomes == [reason]
    errors = [event for event in events if isinstance(event, AgentError)]
    if fault in {"state_fail", "result_fail"}:
        assert errors[0].kind == "OSError"
        assert (
            errors[0].message
            == {"state_fail": "required state write failed", "result_fail": "required tool-result write failed"}[fault]
        )
    else:
        assert errors == []
    rows = await agent.store.load("session")
    # Cleanup may save valid dirty state after abort, but it must not commit the
    # still-unexecuted tool results or report a successful hook-end outcome.
    expected_rows = ["input"]
    if stage != "before_model":
        expected_rows.append("response")
    if fault != "state_fail":
        expected_rows.append("agent_state")
    result_count = 0
    if has_tools:
        result_count = 2 if fault == "clean" else 1 if fault in {"abort_during", "abort_after"} else 0
        expected_rows.extend(["tool_result"] * result_count)
    assert [row.kind for row in rows] == expected_rows
    assert agent.snapshot().state == ({} if fault == "state_fail" else {"ended_at": stage})
    assert [event.event_id for event in events if isinstance(event, ToolFinished)] == [
        row.row_id for row in rows if row.kind == "tool_result"
    ]
    assert len(provider.requests) == (0 if stage == "before_model" else 1)
    assert len(tool.calls) == (1 if stage == "after_tool" else 0)
    project(rows)


@pytest.mark.parametrize(
    "source,invalid",
    [(source, invalid) for source in ("response", "partial") for invalid in ("duplicate_calls", "large_ref")]
    + [(source, "large_ref") for source in ("tool", "after_tool", "terminating_tool", "input", "steer", "follow_up")],
)
async def test_message_admission_matches_projection_and_a_fresh_agent_can_resume(source, invalid):
    """Every writer rejects projection poison before the durable write.

    The old response-only table missed tools, hook rewrites and consumed
    inputs; each could persist a schema-valid ref that blocks every fresh run.
    """
    store = InMemoryTranscriptStore()
    await store.consume_input("session", "prior", UserMessage((text("preserved history"),)))
    baseline = await store.load("session")
    content = (TextBlock(ref=LargeRef("sha256:" + "a" * 64, 1)),)
    bad_input = AgentInput("bad-input", UserMessage(content))
    message = assistant(calls=[ToolCallBlock("same", "echo"), ToolCallBlock("same", "echo")])
    if invalid == "large_ref":
        message = replace(message, content=content)
    bad_result = ToolResult(content, terminate=source == "terminating_tool")
    has_tools = source in {"tool", "after_tool", "terminating_tool"}
    queued = source in {"steer", "follow_up"}

    async def execute(arguments, ctx):
        if ctx.tool_call_id == "bad" and source != "after_tool":
            return bad_result
        return ToolResult((text(ctx.tool_call_id),))

    class Override(Hooks):
        async def after_tool(self, call, result, ctx):
            if source == "after_tool" and call.id == "bad":
                return AlterResult(bad_result)

    async def queue_input(request, cancel):
        assert await getattr(agent, source)(bad_input)
        yield Done(assistant())

    if has_tools:
        scripts = [
            [Done(assistant(calls=[ToolCallBlock(name, "echo") for name in ("good", "bad", "later")]))],
            [Done(assistant())],
        ]
    elif queued:
        scripts = [queue_input, [Done(assistant())]]
    else:
        terminal = (
            ProviderError("server", "partial error", False, partial=message) if source == "partial" else Done(message)
        )
        scripts = [[terminal]]
    provider, tool = ScriptedProvider(scripts), FakeTool(execute=execute)
    agent = agent_for(provider, store=store, tools=[tool], hooks=[Override()])
    incoming = bad_input if source == "input" else input_row("input", "hello")
    events = [event async for event in agent.run(incoming, turn_id="turn")]
    assert events[-1].reason == "error"
    expected_kind = "ProviderProtocolViolation" if source in {"response", "partial"} else "ProjectionError"
    assert [event.kind for event in events if isinstance(event, AgentError)] == [expected_kind]
    rows = await store.load("session")
    assert rows[: len(baseline)] == baseline
    expected_kinds = ["input"]
    if source != "input":
        expected_kinds.append("input")
    if has_tools or queued:
        expected_kinds.append("response")
    if has_tools:
        expected_kinds.append("tool_result")
    assert [row.kind for row in rows] == expected_kinds
    assert [ctx.tool_call_id for _, ctx in tool.calls] == (["good", "bad"] if has_tools else [])
    assert [event.message_id for event in events if isinstance(event, MessageCommitted)] == [
        row.row_id for row in rows if row.kind == "response"
    ]
    assert [event.event_id for event in events if isinstance(event, ToolFinished)] == [
        row.row_id for row in rows if row.kind == "tool_result"
    ]
    assert len(provider.requests) == (0 if source == "input" else 1)
    assert await agent.take_pending_inputs() == ((bad_input,) if queued else ())
    project(rows)
    fresh = agent_for(ScriptedProvider([[Done(assistant())]]), store=agent.store)
    assert (await collect(fresh, "next"))[-1].reason == "completed"


@pytest.mark.parametrize(
    "elapsed,retries,retry_after,expected",
    [
        (0, 0, None, 0.5),
        (0, 2, None, 2),
        (0, 3, None, None),
        (119, 0, 2, None),
        (118, 0, 1, 1),
        (120, 0, None, None),
    ],
)
def test_retry_policy_enforces_c2_count_and_elapsed_bounds(elapsed, retries, retry_after, expected):
    # Existing retry tests only bounded the count; Retry-After could exceed the
    # entire C-2 elapsed budget. No wall clock or network is used here.
    policy = RetryPolicy(max_retries=3)
    error = ProviderError("rate_limit", "busy", True, retry_after_s=retry_after)
    assert policy.delay(error, retries=retries, streamed=False, elapsed_s=elapsed) == expected


@pytest.mark.parametrize("kwargs", [{"max_retries": 4}, {"max_elapsed_s": 121}, {"max_elapsed_s": float("nan")}])
def test_retry_policy_configuration_cannot_weaken_contract(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


@pytest.mark.parametrize(
    "expiration,elapsed",
    [
        ("retry_after", 121),
        ("sleep", 119),
        ("sleep", 120),
        ("sleep", 121),
        ("route", 119),
        ("route", 120),
        ("route", 121),
    ],
)
@pytest.mark.parametrize("hook_action", ["end", "raise", "mutate"])
async def test_retry_deadline_owns_admission_before_rehydration_and_hooks(
    monkeypatch, expiration, elapsed, hook_action
):
    # Provider-count-only assertions missed callbacks and persisted side effects
    # after a slow route resolution had already exhausted the retry budget.
    now = [0.0]
    sleeps = []
    hook_times = []
    rehydration_times = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        sleeps.append(delay)
        if expiration == "sleep":
            now[0] = elapsed
        await real_sleep(0)

    class Router(FakeModelRouter):
        async def resolve(self):
            result = await super().resolve()
            if expiration == "route" and self.resolutions == 2:
                now[0] = elapsed
            return result

    class Hook(Hooks):
        async def before_model(self, request, ctx):
            hook_times.append(now[0])
            if len(hook_times) == 2:
                ctx.state["retry_hook"] = True
                if hook_action == "end":
                    return End()
                if hook_action == "raise":
                    raise ValueError("retry hook failure")

    def rehydrate(state):
        rehydration_times.append(now[0])
        return ()

    monkeypatch.setattr("core.agent_core.agent.loop.time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", sleep)
    error = ProviderError(
        "rate_limit", "original busy error", True, retry_after_s=121 if expiration == "retry_after" else 1
    )
    provider = ScriptedProvider([[error], [Done(assistant())]])
    agent = agent_for(provider, hooks=[Hook()])
    agent.models = Router(provider)
    agent.rehydrate = rehydrate
    events = await collect(agent)
    expired = expiration == "retry_after" or elapsed >= 120
    if expired:
        assert events[-1].reason == "error"
        assert [(e.kind, e.message) for e in events if isinstance(e, AgentError)] == [
            ("rate_limit", "original busy error")
        ]
        assert hook_times == rehydration_times == [0]
        assert agent.snapshot().state == {}
        assert [row.kind for row in await agent.store.load("session")] == ["input"]
    else:
        assert hook_times == rehydration_times == [0, elapsed]
        assert agent.snapshot().state == {"retry_hook": True}
        assert events[-1].reason == {"end": "ended_by_hook", "raise": "error", "mutate": "completed"}[hook_action]
        assert [(e.kind, e.message) for e in events if isinstance(e, AgentError)] == (
            [("ValueError", "retry hook failure")] if hook_action == "raise" else []
        )
    assert len(provider.requests) == (2 if not expired and hook_action == "mutate" else 1)
    assert sleeps == ([] if expiration == "retry_after" else [1])
