"""C-5 orphan/ancestry projection risks not covered by message serialization."""

from copy import deepcopy
from dataclasses import replace

import pytest

from core.agent_core.harness import projection as projection_module
from core.agent_core.harness.projection import (
    INTERRUPTED,
    ProjectionError,
    context_view,
    open_tool_calls,
    project,
    validate_message_append,
)
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import (
    HostedAction,
    HostedNative,
    HostedRecord,
    HostedSummary,
    HostedToolBlock,
    ImageBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_to_dict,
    text,
)
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
        # Every checkpoint row records the inputs it keeps, and a kept input must be an input row before the kept rows.
        ContextEntry(
            "session", 2, "compaction", "checkpoint",
            payload={key: value for key, value in _checkpoint_payload(1).items() if key != "kept_inputs"},
        ),
        _checkpoint_row(2, 1, kept_inputs=[1]),
        _checkpoint_row(2, 2, kept_inputs=[9]),
        _checkpoint_row(2, 2, kept_inputs=["1"]),
        _checkpoint_row(2, 1, summarized_to_seq="1"),
        _checkpoint_row(2, 1, threshold=1.5),
        _checkpoint_row(2, 1, summarizer={"origin": {}, "prompt_version": "v", "rounds": 0}),
        _checkpoint_row(2, 1, usage={"input_tokens": -1}),
        _checkpoint_row(2, 1, unexpected=True),
        ContextEntry("session", 2, "agent_state", "state", payload={"version": 1, "state": {}, "unexpected": True}),
    ],
)
@pytest.mark.parametrize("validation_only", [False, True], ids=["project", "append-validation"])
def test_malformed_context_rows_fail_explicitly_instead_of_projecting_a_wrong_context(row, validation_only):
    rows = [ContextEntry("session", 1, "input", "in-1", user("start")), row]
    before = deepcopy(rows)
    with pytest.raises(ProjectionError):
        if validation_only:
            validate_message_append(rows, session_id="session", kind="input", message=user("next"))
        else:
            project(rows)
    assert rows == before


def test_a_complete_checkpoint_row_loads():
    rows = [
        ContextEntry("session", 1, "input", "in-1", user("start")),
        ContextEntry("session", 2, "agent_state", "state", payload={"version": 1, "state": {}}),
        _checkpoint_row(3, 1, summarizer=None),
    ]
    projected = project(rows)
    assert [block.text for block in projected.messages[0].content] == ["SUMMARY", "STATE"]


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
@pytest.mark.parametrize("validation_only", [False, True], ids=["project", "append-validation"])
def test_projection_revalidates_mutable_arguments_in_loaded_ancestry(arguments, validation_only):
    # Admission cannot protect rows supplied by a store/fork loader from later
    # mutation. Projection must reject them without normalizing the input.
    call = ToolCallBlock("call", "echo")
    call.arguments.update(arguments)
    rows = [ContextEntry("parent", 1, "response", "response", assistant(calls=[call]))]
    original = deepcopy(rows)
    with pytest.raises(ProjectionError, match="call.*response"):
        if validation_only:
            validate_message_append(rows, session_id="child", kind="input", message=user("next"))
        else:
            project(rows)
    assert rows == original


def _mutable_rows():
    """Small graph covering nested mutable fields even on frozen canonical messages."""
    call = ToolCallBlock("call", "echo", {"nested": {"values": [1]}}, signature="signed-call")
    hosted = HostedToolBlock(
        "search", "web_search", HostedAction("search", queries=("query",)), HostedRecord("completed"),
        HostedNative("src_abcdefgh", ({"nested": {"values": [1]}},)),
    )
    response = replace(
        assistant(calls=[call]),
        content=(ThinkingBlock("reasoning", signature="signed-thinking"), call, hosted),
        usage=Usage(cache_write_tokens=3, cache_writes_by_ttl={"5m": 1}),
        hosted_summary=HostedSummary(counts={"searches": 1}),
    )
    return [
        ContextEntry("parent", 1, "response", "response", response, payload={"extra": {"values": [1]}}),
        ContextEntry("child", 2, "agent_state", "state", payload={"version": 1, "state": {"values": [1]}}),
    ]


def _mutate_message(message):
    message.tool_calls[0].arguments["nested"]["values"].append(2)
    message.content[2].native.payload[0]["nested"]["values"].append(2)
    message.hosted_summary.counts["searches"] = 2
    message.usage.cache_writes_by_ttl["5m"] = 2


@pytest.mark.parametrize("boundary", ["context_view", "project", "rehydrated", "open_tool_calls"])
def test_public_projection_boundaries_detach_nested_messages_and_metadata_in_both_directions(boundary):
    # A borrowed validation builder must never accidentally become a public return path.
    rows = _mutable_rows()
    before = deepcopy(rows)
    original_message = rows[0].message
    original_wire = message_to_dict(original_message)
    if boundary == "context_view":
        result = context_view(rows)
        message = result.messages[0]
        entry = result.units[0].lead
        assert entry is not rows[0] and entry.message is message
        entry.payload["extra"]["values"].append(2)
        result.state["values"].append(2)
        assert context_view(rows).state == {"values": [1]}
    elif boundary == "project":
        result = project(rows)
        message = result.messages[0]
        result.state["values"].append(2)
        assert project(rows).state == {"values": [1]}
    elif boundary == "rehydrated":
        result = project([], rehydrated=[original_message])
        message = result.messages[0]
    else:
        ((entry, call),) = open_tool_calls(rows)
        message = entry.message
        assert entry is not rows[0] and call is message.tool_calls[0]
        entry.payload["extra"]["values"].append(2)
    assert message_to_dict(message) == original_wire
    assert message.usage.cache_writes_by_ttl == {"5m": 1}
    _mutate_message(message)
    assert rows == before
    # Usage TTL diagnostics intentionally do not participate in dataclass equality/serialization.
    assert original_message.usage.cache_writes_by_ttl == {"5m": 1}
    assert message_to_dict(project(rows).messages[0]) == original_wire
    assert message_to_dict(open_tool_calls(rows)[0][0].message) == original_wire
    detached_wire = message_to_dict(message)
    original_message.tool_calls[0].arguments["nested"]["values"].append(3)
    original_message.content[2].native.payload[0]["nested"]["values"].append(3)
    original_message.hosted_summary.counts["searches"] = 3
    original_message.usage.cache_writes_by_ttl["5m"] = 3
    rows[0].payload["extra"]["values"].append(3)
    rows[1].payload["state"]["values"].append(3)
    if boundary in {"context_view", "project"}:
        assert result.state == {"values": [1, 2]}
    if boundary in {"context_view", "open_tool_calls"}:
        assert entry.payload["extra"]["values"] == [1, 2]
    assert message_to_dict(message) == detached_wire
    assert message.usage.cache_writes_by_ttl == {"5m": 2}


def test_context_view_preserves_compaction_alias_but_detaches_edited_unit_rows():
    rows = _checkpointed_rows()
    before = deepcopy(rows)
    view = context_view(rows)
    assert view.compaction is rows[8]  # Existing contract; not a deeply detached ContextView as a whole.
    result_row, shown = view.units[1].entries[1]
    assert result_row is not rows[5]
    assert result_row.message.content[0].text == "B"
    assert shown.content[0].text == "latest placeholder"
    result_row.payload["local-only"] = [1]
    assert rows == before
    assert context_view(rows).units[1].entries[1][0].payload == {}


@pytest.mark.parametrize("case,kind,error", [
    ("input", "input", None),
    ("response", "response", None),
    ("late-result", "tool_result", None),
    ("duplicate-open-call", "response", "duplicate open tool call id"),
    ("wrong-name", "tool_result", "result identity does not match"),
    ("unknown-call", "tool_result", "no preceding call"),
    ("duplicate-result", "tool_result", "duplicate tool result"),
    ("duplicate-sequence", "input", "duplicate context_seq"),
    ("invalid-candidate-arguments", "response", "invalid arguments for tool call next"),
])
def test_append_validation_checks_candidate_and_ancestry_without_mutation(case, kind, error):
    rows = _mutable_rows()
    message = {
        "input": user("next"),
        "response": assistant(calls=[ToolCallBlock("next", "echo", {"nested": [1]})]),
        "tool_result": ToolResultMessage("call", "echo", (text("result"),)),
    }[kind]
    if case == "duplicate-open-call":
        message = assistant(calls=[ToolCallBlock("call", "echo")])
    elif case == "wrong-name":
        message = ToolResultMessage("call", "other", (text("result"),))
    elif case == "unknown-call":
        message = ToolResultMessage("unknown", "echo", (text("result"),))
    elif case == "duplicate-result":
        rows.append(ContextEntry("parent", 3, "tool_result", "existing-result", message))
    elif case == "duplicate-sequence":
        rows.append(ContextEntry("parent", 1, "input", "duplicate-seq", user("duplicate")))
    elif case == "invalid-candidate-arguments":
        message.tool_calls[0].arguments["nested"] = (1, 2)
    elif case == "late-result":
        rows.append(ContextEntry("child", 3, "input", "later-input", user("next")))
    before = deepcopy((rows, message))
    if error:
        with pytest.raises(ProjectionError, match=error):
            validate_message_append(rows, session_id="child", kind=kind, message=message)
    else:
        assert validate_message_append(rows, session_id="child", kind=kind, message=message) is None
    assert (rows, message) == before


def test_append_validation_does_not_copy_a_discarded_projection(monkeypatch):
    rows = _checkpointed_rows()
    rows.extend(replace(row, context_seq=row.context_seq + 10, row_id="new-" + row.row_id) for row in _mutable_rows())
    copied = []

    def observed_copy(value):
        copied.append(type(value))
        return deepcopy(value)

    monkeypatch.setattr(projection_module, "deepcopy", observed_copy)
    before = deepcopy(rows)
    validate_message_append(rows, session_id="child", kind="input", message=user("next"))
    assert copied == []
    assert rows == before
