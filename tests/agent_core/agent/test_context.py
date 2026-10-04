"""C-9 rules as pure functions (``agent-core-contracts/context.md``).

The loop tests in ``test_compaction.py`` cover when these rules run; these pin
the rules themselves: the estimate, the limits, the cut, what is cleared, and
the checkpoint's model-facing text and row.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import replace
from pathlib import Path

import pytest

from core.agent_core.ai.provider import ModelCapabilities, ModelEndpoint, ModelRequest
from core.agent_core.harness.context import (
    CLEARED_PLACEHOLDER,
    SkillRef,
    budget,
    carried_skills,
    checkpoint_request,
    clearable_results,
    compaction_payload,
    fit_result,
    half_cut,
    checkpoint_max_tokens,
    last_anchor,
    message_tokens,
    normal_cut,
    output_tokens,
    request_facts,
    request_tokens,
    rolling_cut,
    state_cap,
    text_tokens,
    unit_tokens,
)
from core.agent_core.harness.projection import context_view, project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.tools.base import ToolSpec
from core.agent_core.messages import (
    AssistantMessage,
    ImageBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    text,
)
from tests.agent_core.fakes import ENDPOINT, ORIGIN, assistant, user

CONTRACT = Path(__file__).resolve().parents[3] / "docs/plans/agent-core-contracts/context.md"


class Rows:
    """Committed rows built in context order."""

    def __init__(self) -> None:
        self.rows: list[ContextEntry] = []

    def add(self, kind, message=None, *, payload=None, row_id=None) -> ContextEntry:
        seq = len(self.rows) + 1
        row = ContextEntry("session", seq, kind, row_id or f"{kind}-{seq}", message, payload or {})
        self.rows.append(row)
        return row

    def input(self, value: str) -> ContextEntry:
        return self.add("input", user(value))

    def response(self, *calls, value: str = "", usage=None, stop_reason=None) -> ContextEntry:
        message = assistant(value, calls=calls, stop_reason=stop_reason)
        if usage is not None:
            message = AssistantMessage(message.content, message.origin, message.stop_reason, usage=usage)
        return self.add("response", message)

    def result(self, call: ToolCallBlock, value: str, *, skills=()) -> ContextEntry:
        details = {"skills": [{"name": name} for name in skills]} if skills else {}
        return self.add(
            "tool_result", ToolResultMessage(call.id, call.name, (text(value),)), payload={"details": details}
        )

    def tool(self, name: str, value: str, *, call_id: str, skills=(), **arguments) -> ToolCallBlock:
        call = ToolCallBlock(call_id, name, arguments)
        self.response(call)
        self.result(call, value, skills=skills)
        return call


def test_the_estimate_counts_utf8_bytes_so_chinese_is_not_undercounted():
    chinese = "上下文管理在这里决定什么时候压缩对话" * 4  # 72 characters, 216 UTF-8 bytes
    assert len(chinese) == 72
    assert text_tokens(chinese) == 54  # characters / 4 would claim 18
    assert message_tokens(user(chinese)) == 54
    image = UserMessage((TextBlock(text="see"), ImageBlock("image/png", "media-token")))
    assert message_tokens(image) == 1 + 1_600
    call = ToolCallBlock("id", "read", {"path": "中"}, signature="sig")
    reply = AssistantMessage((ThinkingBlock("ab", signature="cdef"), call), ORIGIN, "tool_use")
    replayed = len("ab") + len("cdef") + len("read") + len('{"path":"中"}'.encode()) + len("sig")
    assert message_tokens(reply) == math.ceil(replayed / 4)


def make_request(messages=(), *, system="system", tools=(), endpoint=ENDPOINT, max_tokens=4_096) -> ModelRequest:
    return ModelRequest(endpoint, system, tuple(messages), tuple(tools), max_tokens)


@pytest.mark.parametrize(
    "window,input_limit,hop_output,configured,output,margin,threshold,keep",
    [
        (200_000, None, 32_000, 64_000, 32_000, 8_000, 160_000, 20_000),
        (1_000_000, None, 32_000, 32_000, 32_000, 30_000, 900_000, 20_000),
        (None, None, None, 8_192, 8_192, 8_000, 111_808, 20_000),  # unknown: W = 128,000, O = 8,192
        (400_000, 272_000, 128_000, 128_000, 128_000, 12_000, 132_000, 20_000),
        (32_000, None, 64_000, 4_096, 4_096, 4_000, 23_904, 5_976),  # O is the request's, capped by the Agent
        (8_000, None, 2_000, 2_000, 2_000, 1_000, 5_000, 1_250),  # M is at most W / 8
    ],
)
def test_limits_follow_the_frozen_formula_from_the_route_capabilities(
    window, input_limit, hop_output, configured, output, margin, threshold, keep
):
    capabilities = ModelCapabilities(context_window=window, input_limit=input_limit, max_output_tokens=hop_output)
    assert output_tokens(capabilities, configured) == output
    assert checkpoint_max_tokens(capabilities, configured) == min(16_000, output)
    plan = budget(make_request(system="", max_tokens=output), capabilities, transcript=())
    assert (plan.output, plan.margin, plan.threshold, plan.keep) == (output, margin, threshold, keep)
    limit = input_limit or window or 128_000
    at_limit = budget(make_request(system="x" * 4 * (limit - output - margin), max_tokens=output), capabilities, transcript=())
    assert at_limit.fits and not replace(at_limit, est=at_limit.est + 1).fits


@pytest.mark.parametrize("window", [4_000, 8_000, 12_000, 32_000, 128_000, 1_000_000])
def test_the_threshold_stays_positive_on_every_window(window):
    # With the Agent's output at most a quarter of the window and the margin at most an eighth, T >= 0.625 * W.
    capabilities = ModelCapabilities(context_window=window, max_output_tokens=window // 4)
    plan = budget(make_request(system="", max_tokens=window // 4), capabilities, transcript=())
    assert plan.margin <= window // 8 and plan.threshold >= math.floor(0.625 * window) > 0
    if window >= 64_000:  # large windows are unchanged
        assert plan.margin == max(8_000, math.ceil(0.03 * window))


def _usage(total: int) -> Usage:
    return Usage(input_tokens=total - 10, output_tokens=10)


CAPABILITIES = ModelCapabilities(context_window=32_000, max_output_tokens=4_096)


def _anchored_rows():
    """A read, then a response R billed 5,000 whose row records its request, R's result, and a later reply."""
    rows = Rows()
    rows.input("x" * 400)
    rows.tool("read", "w" * 4_000, call_id="before", path="a")
    read = ToolCallBlock("r", "read", {"path": "f"})
    sent = make_request(context_view(rows.rows).messages)
    anchored = rows.add(
        "response",
        AssistantMessage((read,), ORIGIN, "tool_use", usage=_usage(5_000)),
        payload={"request": request_facts(sent)},
    )
    result = rows.result(read, "y" * 4_000)
    rows.response(value="z" * 40)
    return rows, anchored, result


def _est(rows, request=None):
    view = context_view(rows.rows)
    request = request or make_request(view.messages)
    return budget(request, CAPABILITIES, transcript=view.messages, anchor=last_anchor(rows.rows, view)).est


def _edit_of(entry):
    return {
        "version": 1,
        "target_event_id": entry.row_id,
        "replacement": {"text": CLEARED_PLACEHOLDER},
        "reason": "clear_old_tool_result",
    }


def test_the_anchor_holds_while_the_transcript_up_to_its_response_is_unchanged():
    rows, anchored, result = _anchored_rows()
    messages = context_view(rows.rows).messages
    after = message_tokens(result.message) + message_tokens(messages[-1])
    assert _est(rows) == 5_000 + after
    # Outside the transcript, a change adjusts the anchored usage by its UTF-8/4 delta.
    longer = "system " * 100
    delta = request_tokens(longer, (), ()) - request_tokens("system", (), ())
    assert _est(rows, make_request(messages, system=longer)) == 5_000 + after + delta
    tool = ToolSpec("t", "d" * 400, {})
    tool_delta = request_tokens("system", (tool,), ()) - request_tokens("system", (), ())
    assert _est(rows, make_request(messages, tools=(tool,))) == 5_000 + after + tool_delta
    rehydrated = user("state " * 50)
    assert _est(rows, make_request((rehydrated, *messages))) == 5_000 + after + message_tokens(rehydrated)
    # An edit after the anchored response is counted by its bytes.
    rows.add("context_edit", payload=_edit_of(result))
    cleared = context_view(rows.rows).messages
    assert _est(rows) == 5_000 + message_tokens(cleared[-2]) + message_tokens(cleared[-1])


def test_an_edit_or_checkpoint_before_the_anchor_or_another_route_invalidates_it():
    def whole(rows, request=None):
        request = request or make_request(context_view(rows.rows).messages)
        return request_tokens(request.system, request.tools, request.messages)

    rows, anchored, result = _anchored_rows()
    other_route = ModelEndpoint("anthropic", "http://model.invalid", "other-model", "", provider="test-provider")
    request = make_request(context_view(rows.rows).messages, endpoint=other_route)
    assert _est(rows, request) == whole(rows, request)  # another model's tokenizer
    # A context_edit of a result before the anchored response changes what its usage measured.
    before = next(row for row in rows.rows if row.kind == "tool_result")
    rows.add("context_edit", payload=_edit_of(before))
    assert _est(rows) == whole(rows)
    # A checkpoint that keeps the anchored response verbatim still replaces what came before it.
    rows, anchored, result = _anchored_rows()
    view = context_view(rows.rows)
    cut = next(index for index, unit in enumerate(view.units) if unit.seq == anchored.context_seq)
    payload = compaction_payload(
        view, cut, mode="normal", reason="threshold", checkpoint="checkpoint", skills=(), state=(),
        earlier_record=None, tokens_before=0, threshold=0, summarizer=None, usage=None,
    )
    rows.add("compaction", payload=payload)
    assert context_view(rows.rows).units[0].seq == anchored.context_seq
    assert _est(rows) == whole(rows)
    # A failed or aborted response, or one without usage or request facts, never anchors.
    for message, facts in [
        (AssistantMessage((), ORIGIN, "error", usage=_usage(9_999)), True),
        (AssistantMessage((), ORIGIN, "aborted", usage=_usage(9_999)), True),
        (AssistantMessage((text("a"),), ORIGIN, "stop", usage=Usage()), True),
        (AssistantMessage((text("a"),), ORIGIN, "stop", usage=_usage(9_999)), False),
    ]:
        only = Rows()
        only.input("x")
        only.add("response", message, payload={"request": {"tokens": 1}} if facts else {})
        assert last_anchor(only.rows, context_view(only.rows)) is None


def _random_rows(seed: int) -> list[ContextEntry]:
    rng = random.Random(seed)
    rows = Rows()
    late: list[tuple[ToolCallBlock, str]] = []
    rows.input("start " * rng.randint(1, 40))
    for step in range(rng.randint(2, 25)):
        if rng.random() < 0.3:
            for call, value in late:
                rows.result(call, value)  # recovery settles open calls before the next input
            late.clear()
            rows.input("next " * rng.randint(1, 40))
            continue
        calls = [ToolCallBlock(f"c{step}-{index}", "bash", {"n": index}) for index in range(rng.randint(0, 3))]
        rows.response(*calls, value="thinking " * rng.randint(0, 20))
        for call in calls:
            value = "out " * rng.randint(1, 400)
            if rng.random() < 0.2:
                late.append((call, value))
            else:
                rows.result(call, value)
    for call, value in late:
        rows.result(call, value)
    return rows.rows


def _assert_paired(messages) -> None:
    """Every tool call is followed by exactly its committed results, in call order."""
    index = 0
    while index < len(messages):
        message = messages[index]
        assert not isinstance(message, ToolResultMessage), "a tool result without its call"
        calls = message.tool_calls if isinstance(message, AssistantMessage) else ()
        results = messages[index + 1 : index + 1 + len(calls)]
        assert [result.tool_call_id for result in results] == [call.id for call in calls]
        assert all(result.content[0].text.startswith("out") for result in results)
        index += 1 + len(calls)


@pytest.mark.parametrize("seed", range(150))
def test_a_cut_never_separates_a_tool_call_from_its_result(seed):
    rows = _random_rows(seed)
    view = context_view(rows)
    rng = random.Random(seed)
    cuts = {
        normal_cut(view.units, rng.choice([0, 50, 500, 5_000])),
        half_cut(view.units),
        rolling_cut(view.units, lambda cut: rng.random() < 0.5),
    }
    for cut in cuts - {None}:
        assert 0 < cut < len(view.units)
        assert view.units[cut].lead.kind in {"input", "response"}
        checkpoint = compaction_payload(
            view,
            cut,
            mode="normal",
            reason="threshold",
            checkpoint="SUMMARY",
            skills=(),
            state=(),
            earlier_record=None,
            tokens_before=0,
            threshold=0,
            summarizer=None,
            usage=None,
        )
        checkpoint["summary"] = "SUMMARY"
        compacted = [*rows, ContextEntry("session", len(rows) + 1, "compaction", "checkpoint", payload=checkpoint)]
        messages = project(compacted).messages
        assert messages[0].content[0].text == "SUMMARY"
        _assert_paired(messages[1:])
        assert len(messages) - 1 == sum(len(unit.messages) for unit in view.units[cut:])


@pytest.mark.parametrize(
    "sizes,keep,cut",
    [
        ([10, 10, 10, 10], 25, 2),  # the longest tail of whole units within keep
        ([10, 10, 10, 40], 25, 3),  # at least the last unit, even when it alone is over keep
        ([10, 10], 100, None),  # everything fits the tail: nothing to summarize
        ([30], 10, None),
    ],
)
def test_the_normal_cut_keeps_whole_recent_units_within_keep(sizes, keep, cut):
    rows = Rows()
    for size in sizes:
        rows.input("x" * 4 * size)
    view = context_view(rows.rows)
    assert [unit_tokens(unit) for unit in view.units] == sizes
    assert normal_cut(view.units, keep) == cut


def test_the_half_cut_is_nearest_to_half_and_the_rolling_cut_moves_earlier_until_the_fork_fits():
    rows = Rows()
    for size in (100, 100, 100, 100, 100, 100):
        rows.input("x" * 400 * size)
    units = context_view(rows.rows).units
    assert half_cut(units) == 3
    assert rolling_cut(units, lambda cut: cut <= 1) == 1
    assert rolling_cut(units, lambda cut: False) is None
    assert half_cut(units[:1]) is None


def test_clearing_spares_recent_turns_the_newest_results_and_skill_loads():
    rows = Rows()
    big = "o" * 20_000  # 5,000 tokens each
    rows.input("turn 1")
    old = [rows.tool("bash", big, call_id=f"old-{index}", command="ls") for index in range(9)]
    rows.tool("bash", big, call_id="skill", command="vibe skill load x", skills=("x",))
    rows.tool("write", big, call_id="write", path="f")
    rows.input("turn 2")
    rows.tool("read", big, call_id="recent-1", path="f")
    rows.input("turn 3")
    rows.tool("read", big, call_id="recent-2", path="f")
    view = context_view(rows.rows)
    cleared = {entry.message.tool_call_id for entry in clearable_results(view)}
    # Results after the second-latest input are protected, then the newest 5 eligible results.
    assert cleared == {call.id for call in old[:6]}

    too_small = Rows()
    too_small.input("turn 1")
    for index in range(9):
        too_small.tool("read", "o" * 1_000, call_id=f"small-{index}", path="f")
    too_small.input("turn 2")
    too_small.input("turn 3")
    assert clearable_results(context_view(too_small.rows)) == ()


@pytest.mark.parametrize("window,cap", [(8_000, 800), (200_000, 20_000)], ids=["8K window", "200K window"])
def test_the_state_cap_is_a_tenth_of_the_route_window_up_to_25000(window, cap):
    assert state_cap(replace(CAPABILITIES, context_window=window)) == cap


def test_each_artifact_list_keeps_its_50_most_recent_paths_across_checkpoints():
    rows, payload = Rows(), None
    rows.input("touch many files")
    for start, stop in ((0, 200), (200, 350), (350, 500)):
        for index in range(start, stop):
            rows.tool("read", "r", call_id=f"r{index}", path=f"read/{index:03}.py")
            rows.tool("write", "w", call_id=f"w{index}", path=f"write/{index:03}.py")
        rows.input("next")
        view = context_view(rows.rows)
        payload = compaction_payload(
            view,
            len(view.units) - 1,
            mode="dropped",
            reason="overflow",
            checkpoint="",
            skills=(),
            state=(),
            earlier_record=None,
            tokens_before=1,
            threshold=2,
            summarizer=None,
            usage=None,
        )
        rows.add("compaction", payload=payload)
    # Most recently touched first, 50 kept, the rest counted, in the row and in what the model reads.
    assert payload["files_read"] == [f"read/{index:03}.py" for index in range(499, 449, -1)]
    assert payload["files_modified"] == [f"write/{index:03}.py" for index in range(499, 449, -1)]
    assert (payload["files_read_more"], payload["files_modified_more"]) == (450, 450)
    artifacts = payload["summary"].split("<artifacts>\n", 1)[1].split("</artifacts>", 1)[0]
    assert artifacts.count("- read/") == 50 and artifacts.count("- write/") == 50
    assert artifacts.count("- and 450 more\n") == 2


@pytest.mark.parametrize("segment", ["deep/", "目录/"], ids=["ascii", "cjk"])
def test_an_artifact_path_is_middle_truncated_so_a_list_stays_small(segment):
    rows = Rows()
    rows.input("deep paths")
    paths = [f"{segment * 800}file-{index:02}.py" for index in range(60)]  # near PATH_MAX, whatever the script
    for index, path in enumerate(paths):
        rows.tool("read", "r", call_id=f"r{index}", path=path)
    rows.input("next")
    view = context_view(rows.rows)
    payload = compaction_payload(
        view,
        len(view.units) - 1,
        mode="dropped",
        reason="overflow",
        checkpoint="",
        skills=(),
        state=(),
        earlier_record=None,
        tokens_before=1,
        threshold=2,
        summarizer=None,
        usage=None,
    )
    listed = payload["files_read"]
    assert len(listed) == 50 and payload["files_read_more"] == 10
    # Each path keeps its head and its file name, cut in the middle to 160 UTF-8 bytes on a character boundary.
    assert all(len(path.encode()) <= 160 and "…" in path and path.startswith(segment) for path in listed)
    assert listed[0].endswith("file-59.py")
    artifacts = payload["summary"].split("<artifacts>\n", 1)[1].split("</artifacts>", 1)[0]
    assert text_tokens(artifacts) <= 50 * 41 + 20  # a line: "- ", at most 160 bytes, a newline


def test_the_checkpoint_request_is_the_owner_approved_prompt_verbatim():
    contract = CONTRACT.read_text()
    section = contract[contract.index("## 11. Checkpoint request") :]
    block = re.search(r"```text\n(.*?)\n```", section, re.S).group(1)
    assert checkpoint_request().content[0].text == block


def test_a_checkpoint_carries_files_skills_and_the_split_turn_request_across_checkpoints():
    rows = Rows()
    rows.input("old turn")
    rows.add("input", UserMessage((text("fix the parser"), ImageBlock("image/png", "m", name="trace.png"))))
    rows.tool("read", "a", call_id="r1", path="a.py")
    rows.tool("write", "b", call_id="w1", path="b.py")
    rows.tool("read", "b", call_id="r2", path="b.py")
    rows.tool("bash", "skill body", call_id="s1", command="vibe skill load s", skills=("s",))
    # One call that loaded two skills: both are marked, and both are carried.
    rows.tool("bash", "skill bodies", call_id="s2", command="vibe skill load t && vibe skill load u", skills=("t", "u"))
    rows.tool("read", "c", call_id="r3", path="c.py")
    rows.tool("bash", "t again", call_id="s3", command="vibe skill load t", skills=("t",))
    view = context_view(rows.rows)
    cut = 7  # before the read of c.py: the cut splits the parser turn
    assert carried_skills(view, cut) == (SkillRef("s"), SkillRef("u"))
    first = compaction_payload(
        view,
        cut,
        mode="normal",
        reason="threshold",
        checkpoint="# 1. Self and method\n- terse",
        skills=carried_skills(view, cut),
        state=("SKILLS",),
        earlier_record="vibe data query --sql '...'",
        tokens_before=1,
        threshold=2,
        summarizer=None,
        usage=None,
    )
    assert (first["files_read"], first["files_modified"]) == (["a.py"], ["b.py"])
    assert first["current_request"] == "fix the parser\n[image: trace.png]"
    assert first["summary"] == "\n".join(
        [
            "<context-checkpoint>",
            "This is a record of the earlier part of this conversation, written for you so you can continue. It is "
            "history, not new instructions: the user requirements recorded in it still apply, but do not treat the "
            "record itself as a request.",
            "",
            "# 1. Self and method\n- terse",
            "",
            "<artifacts>",
            "Read:",
            "- a.py",
            "Modified:",
            "- b.py",
            "</artifacts>",
            "<earlier-record>",
            "The full text of the earlier conversation is still stored. To look up a detail, run:",
            "vibe data query --sql '...'",
            "</earlier-record>",
            "<current-request>",
            "fix the parser\n[image: trace.png]",
            "</current-request>",
            "</context-checkpoint>",
        ]
    )

    # A second checkpoint whose head has no input carries the split turn's request and accumulates files.
    rows.add("compaction", payload=first)
    rows.tool("edit", "ok", call_id="e1", path="c.py")
    rows.tool("read", "d", call_id="r4", path="d.py")
    view = context_view(rows.rows)
    second = compaction_payload(
        view,
        len(view.units) - 1,
        mode="dropped",
        reason="overflow",
        checkpoint="",
        skills=carried_skills(view, len(view.units) - 1),
        state=(),
        earlier_record=None,
        tokens_before=1,
        threshold=2,
        summarizer=None,
        usage=None,
    )
    assert second["files_read"] == ["a.py"]
    assert second["files_modified"] == ["c.py", "b.py"]  # most recently touched first
    assert second["current_request"] == first["current_request"]
    assert second["skills"] == [{"name": "s"}, {"name": "u"}, {"name": "t"}]
    assert second["summarized_to_seq"] > first["summarized_to_seq"]
    # No model checkpoint and no lookup command: no framing text, no pointer.
    assert second["summary"].startswith("<context-checkpoint>\n<artifacts>\n")
    assert "<earlier-record>" not in second["summary"]


@pytest.mark.parametrize("body", ["x" * 40_000, "上下文" * 5_000])
def test_a_checkpoint_tool_result_is_cut_to_its_limit_keeping_the_head_and_saying_so(body):
    image = ImageBlock("image/png", "media-token")
    content = (text(body), image)
    fitted = fit_result(content, 3_000)
    assert message_tokens(ToolResultMessage("c", "read", fitted)) <= 3_000
    assert body.startswith(fitted[0].text) and len(fitted[0].text) > 0
    assert image not in fitted  # 1,600 tokens no longer fit after the head
    assert fitted[-1].text.startswith("[Output truncated to fit this checkpoint turn: showing about 3000 of")
    assert fit_result(content, 50_000) == content
