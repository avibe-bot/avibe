"""Recovery owns durable settlement; projection cannot observe live jobs.

These cover the crash windows and fork invariance absent from the old live-job
projection tests: a retry must skip committed outcomes, a partial settlement
must resume in call order, and later job changes cannot rewrite model history.
"""

import pytest

from core.agent_core.agent.hooks import Snapshot
from core.agent_core.agent.jobs import TrackingJobHost
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.recovery import settle_open_calls
from core.agent_core.ai.provider import Done
from core.agent_core.harness.projection import INTERRUPTED, ProjectionError, project
from core.agent_core.messages import LargeRef, TextBlock, ToolCallBlock, ToolResultMessage, text
from core.agent_core.tools.base import CallInstance, JobStatus, ToolResult
from tests.agent_core.fakes import (
    FakeJobHost,
    FakeModelRouter,
    InMemoryTranscriptStore,
    ScriptedProvider,
    assistant,
    input_row,
    user,
)


def _instance(response, call_id):
    return CallInstance(response.session_id, response.row_id, response.context_seq, call_id)


async def _never_rendered(*_args):
    pytest.fail("a call without a job must not invoke the renderer")


def renderer(jobs):
    async def render(call, job_id, status, watch_id):
        output, _ = jobs.output(job_id)
        if status.state == "gone":
            # Only a recorded stop reason gives an ended job a result of its own (bash's ``recovered_result``).
            reason = jobs.stop_reason(job_id)
            return ToolResult((text(f"{output.decode()}; stopped={reason}"),), is_error=True) if reason else None
        content = (
            f"still running, now Watch {watch_id}"
            if watch_id is not None
            else f"{output.decode()}; exit={status.exit_code}"
        )
        return ToolResult(
            (text(content),),
            is_error=status.state == "exited" and status.exit_code != 0,
            details={"full_output_path": jobs.output_path(job_id)},
        )

    return render


@pytest.mark.parametrize(
    "tool_name,state",
    [
        ("bash", "missing"),
        ("write", "missing"),
        ("edit", "missing"),
        ("bash", "never-ran"),
        ("bash", "running"),
        ("bash", "exited"),
        ("bash", "gone"),
        ("bash", "stopped"),
    ],
)
async def test_resume_settles_each_job_state_once_and_projection_never_rechecks_it(tool_name, state):
    # T2 requires uncertainty, not an implied absence of effects, for calls
    # without job state. The previous bash-only table missed write/edit wording.
    store, jobs = InMemoryTranscriptStore(), FakeJobHost()
    call = ToolCallBlock("call", tool_name)
    response = await store.append_response("session", assistant(calls=[call]), final=False)
    job_ids = {}
    if state != "missing":
        job_ids[_instance(response, "call")] = "job_1"
        jobs.states["job_1"] = JobStatus(
            "gone" if state in {"never-ran", "stopped"} else state, exit_code=7 if state == "exited" else None
        )
        jobs.outputs["job_1"] = b"final output"
        if state == "stopped":
            jobs.stop_reasons["job_1"] = "aborted"

    committed = await settle_open_calls(
        session_id="session",
        store=store,
        jobs=jobs,
        job_ids=job_ids,
        render_result=_never_rendered if state == "missing" else renderer(jobs),
    )
    assert len(committed) == 1
    assert (
        committed[0].message.content[0].text
        == {
            "missing": (
                "[tool call interrupted; it may or may not have completed; re-read the file before continuing]"
            ),
            "never-ran": INTERRUPTED,
            "gone": INTERRUPTED,
            "running": "still running, now Watch watch_job_1",
            "exited": "final output; exit=7",
            "stopped": "final output; stopped=aborted",
        }[state]
    )
    assert committed[0].message.is_error == (state != "running")
    assert bool(jobs.watches) == (state == "running")
    assert jobs.starts == jobs.killed == []
    projected = project(await store.load("session"))
    jobs.states["job_1"] = JobStatus("exited", exit_code=0)
    jobs.outputs["job_1"] = b"later output"
    assert (
        await settle_open_calls(
            session_id="session",
            store=store,
            jobs=jobs,
            job_ids=job_ids,
            render_result=renderer(jobs),
        )
        == ()
    )
    assert project(await store.load("session")) == projected


@pytest.mark.parametrize("failure", ["store_write", "unsupported_result"])
async def test_settlement_resume_after_partial_commit_skips_done_calls_and_keeps_call_order(failure):
    # A schema-valid but unsupported renderer result must fail before its
    # append. The prior store-failure-only case could not detect poisoned rows.
    class InterruptedStore(InMemoryTranscriptStore):
        fail = True

        async def append_tool_result(self, session_id, message, *, details):
            if failure == "store_write" and self.fail and message.tool_call_id == "b":
                raise OSError("crashed before the second result commit")
            return await super().append_tool_result(session_id, message, details=details)

    store, jobs = InterruptedStore(), FakeJobHost()
    response = await store.append_response(
        "session",
        assistant(
            calls=[
                ToolCallBlock("a", "bash"),
                ToolCallBlock("b", "bash"),
            ]
        ),
        final=False,
    )
    for name in ("a", "b"):
        jobs.states[name] = JobStatus("exited", exit_code=0)
        jobs.outputs[name] = name.encode()
    ids = {_instance(response, name): name for name in ("a", "b")}

    async def render(call, job_id, status, watch_id):
        if failure == "unsupported_result" and store.fail and call.id == "b":
            return ToolResult((TextBlock(ref=LargeRef("sha256:" + "a" * 64, 1)),))
        return await renderer(jobs)(call, job_id, status, watch_id)

    error_type, error_message = (
        (OSError, "second result") if failure == "store_write" else (ProjectionError, "large-content references")
    )
    with pytest.raises(error_type, match=error_message):
        await settle_open_calls(session_id="session", store=store, jobs=jobs, job_ids=ids, render_result=render)
    assert [row.message.tool_call_id for row in await store.load("session") if row.kind == "tool_result"] == ["a"]
    before_retry = project(await store.load("session"))
    assert before_retry.messages[-1].content[0].text == "[tool call interrupted; no result recorded]"
    jobs.outputs["a"] = b"must not replace committed output"
    store.fail = False
    new_rows = await settle_open_calls(session_id="session", store=store, jobs=jobs, job_ids=ids, render_result=render)
    assert [row.message.tool_call_id for row in new_rows] == ["b"]
    results = [m for m in project(await store.load("session")).messages if isinstance(m, ToolResultMessage)]
    assert [m.content[0].text for m in results] == ["a; exit=0", "b; exit=0"]
    assert (
        await settle_open_calls(session_id="session", store=store, jobs=jobs, job_ids=ids, render_result=render) == ()
    )
    fresh = Agent(
        session_id="session",
        models=FakeModelRouter(ScriptedProvider([[Done(assistant())]])),
        tools=[],
        hooks=[],
        store=store,
        jobs=jobs,
        cwd="/test-owned",
    )
    events = [event async for event in fresh.run(input_row("next", "continue"), turn_id="next")]
    assert events[-1].reason == "completed"


async def test_fork_settlement_uses_parent_job_identity_and_late_rows_project_at_the_call():
    store, jobs = InMemoryTranscriptStore(), FakeJobHost()
    response = await store.append_response("parent", assistant(calls=[ToolCallBlock("a", "bash")]), final=False)
    store.fork(Snapshot("parent", response.context_seq, {}), session_id="child")
    # A prior interrupted runtime may already have admitted another input.
    await store.consume_input("child", "later", user("after the call"))
    jobs.states["job_a"] = JobStatus("exited", exit_code=0)
    jobs.outputs["job_a"] = b"old output"
    await settle_open_calls(
        session_id="child",
        store=store,
        jobs=jobs,
        job_ids={_instance(response, "a"): "job_a"},
        render_result=renderer(jobs),
    )
    child = project(await store.load("child"))
    assert [m.role for m in child.messages] == ["assistant", "tool_result", "user"]
    assert child.messages[1].content[0].text == "old output; exit=0"
    assert project(await store.load("parent")).messages[1].content[0].text == INTERRUPTED
    store.fork(Snapshot("child", child.context_seq, {}), session_id="grandchild")
    jobs.outputs["job_a"] = b"new output"
    assert project(await store.load("grandchild")) == child


async def test_retry_after_handover_commit_failure_reuses_job_identity_without_starting_a_command():
    class InterruptedStore(InMemoryTranscriptStore):
        fail = True

        async def append_tool_result(self, session_id, message, *, details):
            if self.fail:
                raise OSError("commit interrupted")
            return await super().append_tool_result(session_id, message, details=details)

    class Host(FakeJobHost):
        def __init__(self):
            super().__init__()
            self.handovers = []

        async def hand_over(self, job_id):
            self.handovers.append(job_id)
            return await super().hand_over(job_id)

    store, jobs = InterruptedStore(), Host()
    response = await store.append_response("session", assistant(calls=[ToolCallBlock("a", "bash")]), final=False)
    jobs.states["job_a"] = JobStatus("running")
    jobs.outputs["job_a"] = b""
    with pytest.raises(OSError, match="commit interrupted"):
        await settle_open_calls(
            session_id="session",
            store=store,
            jobs=jobs,
            job_ids={_instance(response, "a"): "job_a"},
            render_result=renderer(jobs),
        )
    watch = jobs.watches["job_a"]
    store.fail = False
    rows = await settle_open_calls(
        session_id="session",
        store=store,
        jobs=jobs,
        job_ids={_instance(response, "a"): "job_a"},
        render_result=renderer(jobs),
    )
    assert len(rows) == 1
    assert rows[0].payload["details"]["watch_id"] == watch
    # Actual Watch deduplication belongs to JobHost, not this fake-backed test.
    assert jobs.handovers == ["job_a", "job_a"]
    assert jobs.starts == []


async def test_tracking_host_forwards_the_full_output_path_and_relative_deadline():
    class Host(FakeJobHost):
        async def wait(self, job_id, *, deadline_s):
            assert (job_id, deadline_s) == ("job_a", 0.25)
            return JobStatus("running")

    host = Host()
    tracked = TrackingJobHost(host)
    assert tracked.output_path("job_a") == "/test-owned/jobs/job_a/output.log"
    assert await tracked.wait("job_a", deadline_s=0.25) == JobStatus("running")


async def test_foreground_release_is_scoped_to_call_then_session_and_never_watch():
    # A single Agent's sequential batch cannot reach shared-host cross-Session
    # ownership; protect that distinct boundary directly on the real wrapper.
    host = FakeJobHost()
    tracked = TrackingJobHost(host)
    for session, call in [("one", "a"), ("one", "b"), ("two", "a"), ("one", "a")]:
        instance = CallInstance(session, "msg_1", 1, call)
        await tracked.start("fake", cwd="/test-owned", env={}, timeout_s=None, call=instance)
    await tracked.hand_over("job_4")
    await tracked.kill_foreground("one", tool_call_id="a")
    assert host.killed == ["job_1"]
    assert host.status("job_2").state == host.status("job_3").state == host.status("job_4").state == "running"
    await tracked.kill_foreground("one")
    assert host.killed == ["job_1", "job_2"]
    assert host.status("job_3").state == host.status("job_4").state == "running"
