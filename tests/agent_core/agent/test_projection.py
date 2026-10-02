"""C-5 orphan/ancestry projection risks not covered by message serialization."""

from copy import deepcopy

import pytest

from core.agent_core.harness.projection import INTERRUPTED, ProjectionError, project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from tests.agent_core.fakes import assistant, user


def test_unsettled_projection_is_deterministic_and_preserves_signed_calls():
    call = ToolCallBlock("call", "bash", signature="opaque Gemini signature")
    owner = ContextEntry("parent", 1, "response", "response", assistant(calls=[call]))
    rows = [owner]
    before = deepcopy(rows)
    projected = project(rows)
    assert projected == project(rows)
    assert projected.messages[0].tool_calls[0].signature == "opaque Gemini signature"
    assert projected.messages[1].content[0].text == INTERRUPTED
    assert projected.messages[1].is_error
    assert rows == before


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


@pytest.mark.parametrize("arguments", [{"nested": {1: "value"}}, {"nested": (1, 2)}])
def test_projection_revalidates_mutable_arguments_in_loaded_ancestry(arguments):
    # Admission cannot protect rows supplied by a store/fork loader from later
    # mutation. Projection must reject them without normalizing the input.
    call = ToolCallBlock("call", "echo")
    call.arguments.update(arguments)
    rows = [ContextEntry("parent", 1, "response", "response", assistant(calls=[call]))]
    original = deepcopy(rows)
    with pytest.raises(ProjectionError, match="call.*response"):
        project(rows)
    assert rows == original
