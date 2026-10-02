"""C-5 orphan/ancestry projection risks not covered by message serialization."""

from copy import deepcopy

import pytest

from core.agent_core.harness.projection import INTERRUPTED, JobOrphanSettler, ProjectionError, project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from core.agent_core.tools.base import JobStatus
from tests.agent_core.fakes import FakeJobHost, assistant, user


@pytest.mark.parametrize("state", ["missing", "running", "exited", "gone"])
def test_orphan_settlement_uses_status_without_starting_or_killing_a_job(state):
    jobs = FakeJobHost()
    call = ToolCallBlock("call", "bash")
    owner = ContextEntry("parent", 1, "response", "response", assistant(calls=[call]))
    job_ids = {}
    if state != "missing":
        job_ids[("parent", "call")] = "job_1"
        jobs.states["job_1"] = JobStatus(state, exit_code=7 if state == "exited" else None)
        jobs.outputs["job_1"] = b"final output"

    def running(job_id, call):
        return ToolResultMessage(call.id, call.name, (text("still running, now Watch w1"),))

    def exited(job_id, status, call):
        output, _ = jobs.output(job_id)
        return ToolResultMessage(
            call.id, call.name, (text(f"{output.decode()}; exit={status.exit_code}"),), is_error=status.exit_code != 0
        )

    rows = [owner]
    before = deepcopy(rows)
    projected = project(rows, settle_orphan=JobOrphanSettler(jobs, job_ids, running, exited))
    assert (
        projected.messages[1].content[0].text
        == {
            "missing": INTERRUPTED,
            "gone": INTERRUPTED,
            "running": "still running, now Watch w1",
            "exited": "final output; exit=7",
        }[state]
    )
    assert projected.messages[1].is_error == (state != "running")
    assert rows == before
    assert jobs.starts == jobs.killed == []


def test_projection_sorts_rows_cuts_at_fork_restores_state_and_places_results_in_call_order():
    calls = [ToolCallBlock("a", "echo"), ToolCallBlock("b", "echo")]
    rows = [
        ContextEntry("parent", 1, "input", "input", user("start")),
        ContextEntry("parent", 2, "response", "response", assistant(calls=calls)),
        ContextEntry("parent", 3, "tool_result", "result-b", ToolResultMessage("b", "echo", (text("b"),))),
        ContextEntry("parent", 4, "agent_state", "state", payload={"version": 1, "state": {"counter": [1]}}),
        ContextEntry("child", 5, "input", "child-input", user("child")),
        ContextEntry("parent", 6, "context_edit", "later-edit", payload={}),
    ]
    output = project(list(reversed(rows)), fork_point=5, system="rebuilt system", rehydrated=[user("rehydrated")])
    assert output.system == "rebuilt system"
    assert output.context_seq == 5
    assert [getattr(message, "tool_call_id", None) for message in output.messages] == [None, None, None, "a", "b", None]
    assert output.messages[3].content[0].text == INTERRUPTED
    assert output.messages[4].content[0].text == "b"
    output.state["counter"].append(2)
    assert rows[3].payload["state"] == {"counter": [1]}


@pytest.mark.parametrize("kind", ["compaction", "context_edit"])
def test_P3_context_rows_fail_explicitly_instead_of_silently_replaying_old_context(kind):
    with pytest.raises(ProjectionError, match=f"{kind} requires P3"):
        project([ContextEntry("session", 1, kind, "unsupported", payload={})])


def test_malformed_or_duplicate_results_are_not_silently_dropped():
    result = ContextEntry("session", 2, "tool_result", "result", ToolResultMessage("a", "echo", (text("a"),)))
    with pytest.raises(ProjectionError, match="no preceding call"):
        project([result])
    response = ContextEntry("session", 1, "response", "response", assistant(calls=[ToolCallBlock("a", "echo")]))
    with pytest.raises(ProjectionError, match="duplicate tool result"):
        project([response, result, ContextEntry("session", 3, "tool_result", "duplicate", result.message)])
