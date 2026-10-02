"""Primary outcomes survive independent cleanup; bad output stays uncommitted.

Previous lifecycle tests covered admission/release, not the primary x cleanup
cross product or the gap between receiving a terminal and committing it.
"""

import asyncio
from dataclasses import replace

import pytest

from core.agent_core.agent.events import AgentError, MessageCommitted, RunEnded, ToolFinished
from core.agent_core.agent.hooks import End, Hooks
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import RetryPolicy
from core.agent_core.ai.provider import Done, ProviderError
from core.agent_core.harness.projection import project
from core.agent_core.messages import LargeRef, TextBlock, ToolCallBlock
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


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("invalid", ["duplicate_calls", "large_ref"])
async def test_response_admission_matches_projection_and_a_fresh_agent_can_resume(partial, invalid):
    message = assistant(calls=[ToolCallBlock("same", "echo"), ToolCallBlock("same", "echo")])
    if invalid == "large_ref":
        message = replace(message, content=(TextBlock(ref=LargeRef("sha256:" + "a" * 64, 1)),))
    terminal = ProviderError("server", "partial error", False, partial=message) if partial else Done(message)
    provider, tool = ScriptedProvider([[terminal]]), FakeTool()
    agent = agent_for(provider, tools=[tool])
    events = await collect(agent)
    assert events[-1].reason == "error"
    assert any(isinstance(event, AgentError) and event.kind == "ProviderProtocolViolation" for event in events)
    assert [row.kind for row in await agent.store.load("session")] == ["input"]
    assert tool.calls == []
    assert not any(isinstance(event, MessageCommitted) for event in events)
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


@pytest.mark.parametrize("expiration", ["retry_after", "sleep", "route"])
async def test_retry_does_not_start_a_provider_attempt_after_deadline(monkeypatch, expiration):
    now = [0.0]
    sleeps = []
    real_sleep = asyncio.sleep

    async def sleep(delay):
        sleeps.append(delay)
        if expiration == "sleep":
            now[0] = 121
        await real_sleep(0)

    class Router(FakeModelRouter):
        async def resolve(self):
            result = await super().resolve()
            if expiration == "route" and self.resolutions == 2:
                now[0] = 121
            return result

    monkeypatch.setattr("core.agent_core.agent.loop.time.monotonic", lambda: now[0])
    monkeypatch.setattr("core.agent_core.agent.loop.asyncio.sleep", sleep)
    error = ProviderError(
        "rate_limit", "original busy error", True, retry_after_s=121 if expiration == "retry_after" else 1
    )
    provider = ScriptedProvider([[error], [Done(assistant())]])
    agent = agent_for(provider)
    agent.models = Router(provider)
    events = await collect(agent)
    assert len(provider.requests) == 1
    assert events[-1].reason == "error"
    assert [(e.kind, e.message) for e in events if isinstance(e, AgentError)] == [("rate_limit", "original busy error")]
    assert sleeps == ([] if expiration == "retry_after" else [1])
