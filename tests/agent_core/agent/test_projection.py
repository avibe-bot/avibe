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


def _edit(seq, row_id, target, replacement):
    payload = {
        "version": 1,
        "target_event_id": target,
        "replacement": {"text": replacement},
        "reason": "clear_old_tool_result",
    }
    return ContextEntry("session", seq, "context_edit", row_id, payload=payload)


def _checkpoint_row(seq, first_kept_seq, *, summary="SUMMARY", state=("STATE",)):
    payload = {"version": 1, "summary": summary, "state": list(state), "first_kept_seq": first_kept_seq}
    return ContextEntry("session", seq, "compaction", f"checkpoint-{seq}", payload=payload)


def _checkpointed_rows():
    read, bash = ToolCallBlock("a", "read", {"path": "x"}), ToolCallBlock("b", "bash", {"command": "ls"})
    return [
        ContextEntry("session", 1, "input", "in-1", user("start")),
        ContextEntry("session", 2, "response", "r-1", assistant(calls=[read])),
        ContextEntry("session", 3, "input", "in-2", user("next")),
        ContextEntry("session", 4, "response", "r-2", assistant(calls=[bash])),
        # A late recovery result of a call the checkpoint summarizes.
        ContextEntry("session", 5, "tool_result", "res-a", ToolResultMessage("a", "read", (text("A"),))),
        ContextEntry("session", 6, "tool_result", "res-b", ToolResultMessage("b", "bash", (text("B"),), True)),
        _edit(7, "edit-1", "res-b", "first placeholder"),
        _edit(8, "edit-2", "res-b", "latest placeholder"),
        _checkpoint_row(9, 3, state=("STATE-1", "STATE-2")),
        ContextEntry("session", 10, "input", "in-3", user("after")),
    ]


def test_checkpoints_and_edits_project_purely_and_deterministically():
    rows = _checkpointed_rows()
    before = deepcopy(rows)
    projected = project(list(reversed(rows)), system="system", rehydrated=[user("rehydrated")])
    assert projected == project(rows, system="system", rehydrated=[user("rehydrated")])
    assert rows == before
    checkpoint, *rest = projected.messages[1:]
    assert [block.text for block in checkpoint.content] == ["SUMMARY", "STATE-1", "STATE-2"]
    assert [type(message).__name__ for message in rest] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "UserMessage",
    ]
    cleared = rest[2]
    assert (cleared.tool_call_id, cleared.tool_name, cleared.is_error) == ("b", "bash", True)
    assert cleared.content[0].text == "latest placeholder"
    # The summarized call's late result left with it: no orphan reaches a provider.
    assert all(getattr(message, "tool_call_id", None) != "a" for message in projected.messages)


@pytest.mark.parametrize(
    "fork_point,result_b",
    [(8, "latest placeholder"), (7, "first placeholder"), (6, "B")],
)
def test_a_fork_before_a_checkpoint_or_edit_projects_the_original_context(fork_point, result_b):
    projected = project(_checkpointed_rows(), fork_point=fork_point)
    assert [getattr(message, "tool_call_id", None) for message in projected.messages] == [
        None,
        None,
        "a",
        None,
        None,
        "b",
    ]
    assert projected.messages[2].content[0].text == "A"
    assert projected.messages[5].content[0].text == result_b


@pytest.mark.parametrize(
    "row",
    [
        _edit(2, "edit", "in-1", "x"),
        _edit(2, "edit", "missing", "x"),
        ContextEntry("session", 2, "context_edit", "edit", payload={"version": 1, "target_event_id": "in-1"}),
        _checkpoint_row(2, 3),
        ContextEntry("session", 2, "compaction", "checkpoint", payload={"version": 1, "first_kept_seq": 1}),
    ],
)
def test_malformed_context_rows_fail_explicitly_instead_of_projecting_a_wrong_context(row):
    with pytest.raises(ProjectionError):
        project([ContextEntry("session", 1, "input", "in-1", user("start")), row])


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
