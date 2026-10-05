"""C-5 orphan/ancestry projection risks not covered by message serialization."""

from copy import deepcopy

import pytest

from core.agent_core.harness.projection import INTERRUPTED, ProjectionError, context_view, project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import ImageBlock, ToolCallBlock, ToolResultMessage, UserMessage, text
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


def _checkpoint_payload(first_kept_seq, *, summary="SUMMARY", state=("STATE",), **fields):
    payload = {
        "version": 1,
        "mode": "normal",
        "reason": "threshold",
        "summary": summary,
        "checkpoint": "checkpoint",
        "state": list(state),
        "first_kept_seq": first_kept_seq,
        "summarized_to_seq": first_kept_seq - 1,
        "previous_compaction_id": None,
        "kept_inputs": [],
        "files_read": ["a.py"],
        "files_modified": [],
        "files_modified_omitted": False,
        "files_read_omitted": False,
        "skills_omitted": False,
        "skills": [{"name": "s"}],
        "tokens_before": 10,
        "tokens_after_estimate": 5,
        "threshold": 8,
        "summarizer": {"origin": {"provider": "p", "api": "anthropic", "model": "m"}, "prompt_version": "v", "rounds": 0},
        "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    payload.update(fields)
    return payload


def _checkpoint_row(seq, first_kept_seq, *, summary="SUMMARY", state=("STATE",), **fields):
    payload = _checkpoint_payload(first_kept_seq, summary=summary, state=state, **fields)
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


def _state_row(context):
    return ContextEntry("session", 2, "agent_state", "state", payload={"version": 1, "state": {}, "context": context})


@pytest.mark.parametrize(
    "row",
    [
        _edit(2, "edit", "in-1", "x"),
        _edit(2, "edit", "missing", "x"),
        ContextEntry("session", 2, "context_edit", "edit", payload={"version": 1, "target_event_id": "in-1"}),
        _checkpoint_row(2, 3),
        ContextEntry("session", 2, "compaction", "checkpoint", payload={"version": 1, "first_kept_seq": 1}),
        # Every field the C-9 rules read later is checked when the row loads, not when it is next used.
        _checkpoint_row(2, 1, skills=[{}]),
        _checkpoint_row(2, 1, files_read="a.py"),
        _checkpoint_row(2, 1, files_modified=[1]),
        _checkpoint_row(2, 1, mode="partial"),
        # A kept input must be an input row before the kept rows.
        _checkpoint_row(2, 1, kept_inputs=[1]),
        _checkpoint_row(2, 2, kept_inputs=[9]),
        _checkpoint_row(2, 2, kept_inputs=["1"]),
        _checkpoint_row(2, 1, summarized_to_seq="1"),
        _checkpoint_row(2, 1, threshold=1.5),
        _checkpoint_row(2, 1, summarizer={"origin": {}, "prompt_version": "v", "rounds": 0}),
        _checkpoint_row(2, 1, usage={"input_tokens": -1}),
        _checkpoint_row(2, 1, unexpected=True),
        # Hook state only: C-9 keeps no guard in the rows.
        _state_row({"failures": 0, "ineffective": 0, "paused": False}),
    ],
)
def test_malformed_context_rows_fail_explicitly_instead_of_projecting_a_wrong_context(row):
    with pytest.raises(ProjectionError):
        project([ContextEntry("session", 1, "input", "in-1", user("start")), row])


def test_a_complete_checkpoint_row_loads():
    rows = [
        ContextEntry("session", 1, "input", "in-1", user("start")),
        ContextEntry("session", 2, "agent_state", "state", payload={"version": 1, "state": {}}),
        _checkpoint_row(3, 1, summarizer=None),
    ]
    projected = project(rows)
    assert [block.text for block in projected.messages[0].content] == ["SUMMARY", "STATE"]


def test_a_checkpoint_row_written_before_kept_inputs_keeps_the_pin_it_was_written_with():
    # Rows written before the field pinned the latest input before first_kept_seq when the cut fell inside a turn;
    # without the field, the same rule applies, so such a conversation keeps its request. An explicit empty list
    # pins nothing.
    def legacy(first_kept_seq, **fields):
        payload = {key: value for key, value in _checkpoint_payload(first_kept_seq).items() if key != "kept_inputs"}
        return ContextEntry("session", 9, "compaction", "c", payload={**payload, **fields})

    rows = [
        ContextEntry("session", 1, "input", "in-1", user("an older request")),
        ContextEntry("session", 2, "response", "out-1", assistant("done")),
        ContextEntry("session", 3, "input", "in-2", user("read f10 to f18")),
        ContextEntry("session", 4, "response", "out-2", assistant("f10")),
        ContextEntry("session", 5, "response", "out-3", assistant("f11")),
    ]
    texts = [message.content[0].text for message in project([*rows, legacy(5)]).messages]
    assert texts == ["read f10 to f18", "SUMMARY", "f11"]
    assert context_view([*rows, legacy(5)]).pinned == 1
    # A cut at an input split nothing: nothing is pinned.
    assert [m.content[0].text for m in project([*rows, legacy(3)]).messages][:2] == ["SUMMARY", "read f10 to f18"]
    assert [m.content[0].text for m in project([*rows, legacy(5, kept_inputs=[])]).messages] == ["SUMMARY", "f11"]


def test_the_kept_inputs_of_a_turn_stay_whole_in_order_before_the_checkpoint():
    # The in-flight Turn's input and a steer it accepted, both before the cut: each stays as it was, in order.
    call = ToolCallBlock("r1", "read", {"path": "a.py"})
    rows = [
        ContextEntry("session", 1, "input", "in-1", user("read f10 to f18")),
        ContextEntry("session", 2, "response", "out-1", assistant(calls=[call])),
        ContextEntry("session", 3, "tool_result", "res-1", ToolResultMessage("r1", "read", (text("f10"),))),
        ContextEntry("session", 4, "input", "steer-1", user("also note each file's size")),
        ContextEntry("session", 5, "response", "out-2", assistant("f11 next")),
        _checkpoint_row(6, 5, kept_inputs=[1, 4]),
    ]
    messages = project(rows).messages
    assert [message.content[0].text for message in messages] == [
        "read f10 to f18",
        "also note each file's size",
        "SUMMARY",
        "f11 next",
    ]


def test_a_checkpoint_that_splits_a_turn_keeps_the_turn_input_whole_before_it_and_the_tail():
    # The cut fell inside the turn: the turn's input stays as it was, image included, right before the checkpoint,
    # whose state is then the latest environment the model reads; what came before the input is summarized.
    image = UserMessage(
        (
            text("<environment>\ndate: 2026-10-04\n</environment>"),
            text("what is in this trace?"),
            ImageBlock("image/png", "media-1", name="trace.png"),
        )
    )
    call = ToolCallBlock("r1", "read", {"path": "a.py"})
    rows = [
        ContextEntry("session", 1, "input", "in-1", user("an older turn")),
        ContextEntry("session", 2, "response", "out-1", assistant("done")),
        ContextEntry("session", 3, "input", "in-2", image),
        ContextEntry("session", 4, "response", "out-2", assistant(calls=[call])),
        ContextEntry("session", 5, "tool_result", "res-2", ToolResultMessage("r1", "read", (text("a"),))),
        ContextEntry("session", 6, "response", "out-3", assistant("the trace shows a stall")),
        _checkpoint_row(7, 6, state=("<environment>\ndate: 2026-10-05\n</environment>",), kept_inputs=[3]),
    ]
    messages = project(rows).messages
    assert [type(message).__name__ for message in messages] == ["UserMessage", "UserMessage", "AssistantMessage"]
    assert messages[0] == image and messages[1].content[0].text == "SUMMARY"
    assert messages[2].content[0].text == "the trace shows a stall"
    # The date changed after the input was consumed: the checkpoint's state, read later, says the current one.
    before_tail = "\n".join(getattr(block, "text", None) or "" for message in messages[:2] for block in message.content)
    assert before_tail.rsplit("date: ", 1)[1].startswith("2026-10-05")
    # A cut at an input splits nothing: the checkpoint comes first, then the tail from that input.
    rows[-1] = _checkpoint_row(7, 3)
    messages = project(rows).messages
    assert messages[0].content[0].text == "SUMMARY" and messages[1] == image and len(messages) == 5


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
