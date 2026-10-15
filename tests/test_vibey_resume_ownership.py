"""Retry ownership and compaction pins through the real adapter and durable Delivery rows.

The ordinary resume tests do not cover a returned P1 input, multiple retry Turns,
or process recovery while a transcript-only retry owns a tool call.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

from sqlalchemy import select

from core.agent_core.ai.errors import classify_error
from core.agent_core.ai.provider import Done, ProviderError
from core.agent_core.harness.context import compaction_payload
from core.agent_core.harness.projection import context_view
from core.agent_core.messages import ToolCallBlock, text
from core.agent_core.tools.jobs import JobStatus
from core.agent_core.tools.base import ToolResult
from core.services.agent_steering import ActiveSteerTarget, SteerOutcome, SteerRequest
from modules.agents.vibey.tools import ToolSuite
from storage import message_deliveries, messages_service
from storage.models import agent_events
from tests.agent_core.fakes import FakeJobHost, FakeTool, assistant
from tests.backend_failure_retry_helpers import reserve_failure_retry
from tests.test_vibey_agent import (  # noqa: F401 (fixtures)
    SESSION, _Harness, _context, _turn, engine, published, session,
)


def _retry_request(harness, failed):
    source_turn = _turn(failed.context)
    with harness.engine.begin() as conn:
        message_deliveries.terminalize_turn(
            conn, source_turn, outcome="failed", settled_by="terminal_result", evidence_kind="terminal_result"
        )
        notice = next(row for row in messages_service.list_session_messages(conn, session_id=SESSION)["messages"]
                      if row["type"] == "notify" and row["metadata"].get("turn_id") == source_turn)
        retry = reserve_failure_retry(conn, notice)
        turn_id = message_deliveries.new_turn_id()
        message_deliveries.claim_start_batch(
            conn, turn_id=turn_id, session_id=SESSION, backend="vibey", deliveries=[retry], dispatch_text=""
        )
    payload = message_deliveries.delivery_payload(retry)
    context = _context("avibe", SESSION, turn_id=turn_id, delivery_id=retry["id"])
    context.platform_specific.update({
        "vibey_resume": message_deliveries.is_vibey_resume_payload(payload),
        "vibey_source_turn_id": payload["metadata"][message_deliveries.VIBEY_SOURCE_TURN_METADATA_KEY],
    })
    return replace(failed, context=context, message="", user_message="")


async def _accepted_steer(harness, request, value):
    turn_id = _turn(request.context)
    native = harness.controller.started[-1]
    delivery_id, attempt_id = harness.open_steer(value, turn_id, native)
    receipt = await harness.agent.steer_active_turn(
        SteerRequest(SESSION, turn_id, native, value, attempt_id=attempt_id),
        ActiveSteerTarget("runtime", turn_id, request.context, request, harness.agent),
    )
    assert receipt.outcome is SteerOutcome.ACCEPTED
    harness.accept_steer(delivery_id, attempt_id, turn_id)
    return delivery_id


def _record_agents(monkeypatch, harness):
    agents = []
    start = harness.agent._start_agent

    def recording(turn, *args, **kwargs):
        start(turn, *args, **kwargs)
        agents.append(turn.agent)

    monkeypatch.setattr(harness.agent, "_start_agent", recording)
    return agents


def _kept_after_cut(entries, pins):
    """The actual compaction writer must retain each pinned input across a cut."""
    view = context_view(entries)
    return compaction_payload(
        view, len(view.units) - 1, mode="normal", reason="threshold", checkpoint="task checkpoint",
        state=[], earlier_record=None, tokens_before=30_000, threshold=20_000, window=32_000,
        summarizer=None, usage=None, turn_inputs=pins,
    )["kept_inputs"]


async def test_returned_steer_is_pinned_when_the_failed_request_auto_continues(
    engine, session, tmp_path, published, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail(request, cancel):
        entered.set()
        await release.wait()
        yield classify_error(status=503, body="temporary streaming failure", streamed=True)

    harness = _Harness(engine, tmp_path, "avibe", [fail, [Done(assistant("recovered"))]])
    agents = _record_agents(monkeypatch, harness)
    request = harness.request("修复问题但保留数据")
    running = asyncio.create_task(harness.agent.handle_message(request))
    await entered.wait()
    try:
        steer_id = await _accepted_steer(harness, request, "不要删除凭据")
    finally:
        release.set()
    await running

    entries = await harness.context_rows()
    pins = {row.context_seq for row in entries if row.kind == "input"}
    assert any(row.row_id == steer_id for row in entries)
    assert agents[-1]._turn_inputs == pins
    assert set(_kept_after_cut(entries, agents[-1]._turn_inputs)) == pins
    assert harness.controller.im_client.sent[-1] == "recovered"


async def test_repeated_retry_keeps_original_and_retry_steer_inputs_but_not_old_history(
    engine, session, tmp_path, published, monkeypatch
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def failed_retry(request, cancel):
        entered.set()
        await release.wait()
        yield ProviderError("invalid_request", "failed again", False)

    harness = _Harness(engine, tmp_path, "avibe", [
        [Done(assistant("old task done"))],
        [ProviderError("invalid_request", "failed", False)],
        failed_retry,
        [Done(assistant("recovered"))],
    ])
    agents = _record_agents(monkeypatch, harness)
    await harness.agent.handle_message(harness.request("old unrelated task"))
    failed = harness.request("继续当前任务")
    await harness.agent.handle_message(failed)
    retry = _retry_request(harness, failed)
    running = asyncio.create_task(harness.agent.handle_message(retry))
    await entered.wait()
    try:
        steer_id = await _accepted_steer(harness, retry, "保留数据库")
    finally:
        release.set()
    await running
    again = _retry_request(harness, retry)
    await harness.agent.handle_message(again)

    entries = await harness.context_rows()
    expected = {row.context_seq for row in entries
                if row.row_id in {failed.context.platform_specific["delivery_id"], steer_id}}
    assert len(expected) == 2
    assert agents[-1]._turn_inputs == expected
    assert set(_kept_after_cut(entries, agents[-1]._turn_inputs)) == expected
    assert len(harness.rows("agent_initiated")) == 2
    assert harness.controller.im_client.sent[-1] == "recovered"


async def test_live_retry_attributes_tool_results_and_audits_without_consuming_control_input(
    engine, session, tmp_path, published
):
    harness = _Harness(engine, tmp_path, "avibe", [
        [ProviderError("invalid_request", "failed", False)],
        [Done(assistant("", calls=[ToolCallBlock("retry_tool", "echo", {})]))],
        [Done(assistant("recovered"))],
    ])
    original = harness.request("继续运行检查")
    await harness.agent.handle_message(original)
    retry = _retry_request(harness, original)
    with engine.connect() as conn:
        before = set(conn.execute(select(agent_events.c.id)).scalars())
    await harness.agent.handle_message(retry)

    with engine.connect() as conn:
        events = [row for row in conn.execute(select(agent_events)).mappings() if row["id"] not in before]
    assert any(row["event_type"] == "tool_result" for row in events)
    assert {row["turn_id"] for row in events} == {_turn(retry.context)}
    assert len(harness.rows("user")) == 1
    assert harness.rows("agent_initiated")[0]["context_seq"] is None


async def test_retry_events_and_recovered_job_belong_to_retry_turn_not_last_input(
    engine, session, tmp_path, published
):
    jobs = FakeJobHost()

    async def recovered(call, job_id, status, watch_id):
        return ToolResult((text(f"continued as {watch_id}"),))

    suite = ToolSuite(
        jobs=jobs, create_tools=lambda jobs_, sink: [FakeTool("bash")], render_recovered=recovered,
        find_job=lambda call: "job_retry" if call.tool_call_id == "retry_bash" else None,
    )
    harness = _Harness(engine, tmp_path, "avibe", [[ProviderError("invalid_request", "failed", False)]], suite=suite)
    original = harness.request("运行检查")
    await harness.agent.handle_message(original)
    retry = _retry_request(harness, original)
    retry_turn = _turn(retry.context)
    harness.controller._native_start(retry.context)
    # Simulate the retry process dying after the tool call was committed, before its result.
    harness.agent.store.bind_agent(SESSION, "vibey", turn_id=retry_turn)
    response = await harness.agent.store.append_response(
        SESSION, assistant("", calls=[ToolCallBlock("retry_bash", "bash", {"command": "pytest"})]), final=False
    )
    await harness.agent.store.append_audit(SESSION, "attempt", {"version": 1, "test": "retry"})
    await harness.agent.store.append_payload(SESSION, "agent_state", {"version": 1, "state": {"resumed": True}})
    jobs.states["job_retry"] = JobStatus("running")
    harness.new_agent()
    assert await harness.agent.recover_runtime_state() == []

    with engine.connect() as conn:
        events = conn.execute(select(agent_events).where(agent_events.c.session_id == SESSION)).mappings().all()
    retry_events = [row for row in events if row["context_seq"] is None or row["context_seq"] > response.context_seq]
    assert {row["turn_id"] for row in retry_events} == {retry_turn}
    assert jobs.watches == {"job_retry": "watch_job_retry"}
    [result] = [entry for entry in await harness.context_rows() if entry.kind == "tool_result"]
    assert "watch_job_retry" in result.message.content[0].text
    [response_row] = harness.rows("assistant")
    assert json.loads(response_row["metadata_json"])["turn_id"] == retry_turn
