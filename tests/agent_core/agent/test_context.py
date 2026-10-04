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

from core.agent_core.ai.provider import ModelCapabilities
from core.agent_core.harness.context import (
    SkillRef,
    anchor,
    budget,
    carried_skills,
    checkpoint_request,
    clearable_results,
    compaction_payload,
    fit_result,
    half_cut,
    message_tokens,
    normal_cut,
    request_tokens,
    rolling_cut,
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
from tests.agent_core.fakes import ORIGIN, assistant, user

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

    def result(self, call: ToolCallBlock, value: str, *, skill=None) -> ContextEntry:
        details = {"skill": {"name": skill[0], "revision": skill[1]}} if skill else {}
        return self.add(
            "tool_result", ToolResultMessage(call.id, call.name, (text(value),)), payload={"details": details}
        )

    def tool(self, name: str, value: str, *, call_id: str, skill=None, **arguments) -> ToolCallBlock:
        call = ToolCallBlock(call_id, name, arguments)
        self.response(call)
        self.result(call, value, skill=skill)
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


@pytest.mark.parametrize(
    "window,input_limit,hop_output,configured,output,margin,threshold,keep",
    [
        (200_000, None, 32_000, 64_000, 32_000, 8_000, 160_000, 20_000),
        (1_000_000, None, 32_000, 32_000, 32_000, 30_000, 900_000, 20_000),
        (None, None, None, 8_192, 8_192, 8_000, 111_808, 20_000),  # unknown: W = 128,000, O = 8,192
        (400_000, 272_000, 128_000, 128_000, 128_000, 12_000, 132_000, 20_000),
        (32_000, None, 64_000, 4_096, 4_096, 8_000, 19_904, 4_976),  # O is the request's, capped by the Agent
    ],
)
def test_limits_follow_the_frozen_formula_from_the_route_capabilities(
    window, input_limit, hop_output, configured, output, margin, threshold, keep
):
    capabilities = ModelCapabilities(context_window=window, input_limit=input_limit, max_output_tokens=hop_output)
    plan = budget(system="", tools=(), messages=(), capabilities=capabilities, max_tokens=configured)
    assert (plan.output, plan.margin, plan.threshold, plan.keep) == (output, margin, threshold, keep)
    assert plan.checkpoint_max_tokens == min(16_000, output)
    limit = input_limit or window or 128_000
    at_limit = budget(
        system="x" * 4 * (limit - output - margin),
        tools=(),
        messages=(),
        capabilities=capabilities,
        max_tokens=configured,
    )
    assert at_limit.fits and not replace(at_limit, est=at_limit.est + 1).fits


def _usage(total: int) -> Usage:
    return Usage(input_tokens=total - 10, output_tokens=10)


CAPABILITIES = ModelCapabilities(context_window=32_000, max_output_tokens=4_096)


def test_usage_anchors_the_estimate_only_while_its_request_is_still_the_prefix():
    read = ToolCallBlock("r", "read", {"path": "f"})
    sent = (user("x" * 400),)
    response = AssistantMessage((read,), ORIGIN, "tool_use", usage=_usage(5_000))
    result = ToolResultMessage("r", "read", (text("y" * 4_000),))
    later = assistant("z" * 40)
    current = (*sent, response, result, later)
    made = anchor("system", (), sent, response)
    anchored = budget(system="system", tools=(), messages=current, capabilities=CAPABILITIES, max_tokens=4_096, anchors=(made,))
    assert anchored.est == 5_000 + message_tokens(result) + message_tokens(later)
    full = request_tokens("system", (), current)
    changed = [
        ("other system", (), current),  # the system prompt changed
        ("system", (ToolSpec("t", "d", {}),), current),  # the tool definitions changed
        ("system", (), (user("rehydrated"), *current)),  # something now comes before the transcript
        ("system", (), (user("x" * 399 + "!"), response, result, later)),  # an edit inside the anchored prefix
        ("system", (), sent),  # the response is not in this request
    ]
    for system, tools, messages in changed:
        plan = budget(system=system, tools=tools, messages=messages, capabilities=CAPABILITIES, max_tokens=4_096, anchors=(made,))
        assert plan.est == request_tokens(system, tools, messages)
    assert budget(system="system", tools=(), messages=current, capabilities=CAPABILITIES, max_tokens=4_096).est == full
    # A change after the anchored response is not a prefix change: it is counted by its bytes.
    cleared = (*sent, response, ToolResultMessage("r", "read", (text("cleared"),)), later)
    plan = budget(system="system", tools=(), messages=cleared, capabilities=CAPABILITIES, max_tokens=4_096, anchors=(made,))
    assert plan.est == 5_000 + message_tokens(cleared[2]) + message_tokens(later)
    # A failed or aborted response, or one without usage, never anchors.
    assert anchor("system", (), sent, AssistantMessage((read,), ORIGIN, "error", usage=_usage(9_999))) is None
    assert anchor("system", (), sent, AssistantMessage((read,), ORIGIN, "tool_use", usage=Usage())) is None
    assert anchor("system", (), sent, AssistantMessage((read,), ORIGIN, "tool_use")) is None


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
            focus=None,
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
    rows.tool("bash", big, call_id="skill", command="vibe skill load x", skill=("x", "r1"))
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


def test_the_checkpoint_request_is_the_owner_approved_prompt_verbatim():
    contract = CONTRACT.read_text()
    section = contract[contract.index("## 11. Checkpoint request") :]
    block = re.search(r"```text\n(.*?)\n```", section, re.S).group(1)
    focus_line = "Additional focus from the user: <focus, only for /compact <focus>>\n"
    assert focus_line in block
    assert checkpoint_request().content[0].text == block.replace(focus_line, "")
    focused = checkpoint_request("  keep the migration plan  ").content[0].text
    assert focused == block.replace(focus_line, "Additional focus from the user: keep the migration plan\n")


def test_a_checkpoint_carries_files_skills_and_the_split_turn_request_across_checkpoints():
    rows = Rows()
    rows.input("old turn")
    rows.add("input", UserMessage((text("fix the parser"), ImageBlock("image/png", "m", name="trace.png"))))
    rows.tool("read", "a", call_id="r1", path="a.py")
    rows.tool("write", "b", call_id="w1", path="b.py")
    rows.tool("read", "b", call_id="r2", path="b.py")
    rows.tool("bash", "skill body", call_id="s1", command="vibe skill load s", skill=("s", "1"))
    rows.tool("bash", "skill body", call_id="s2", command="vibe skill load t", skill=("t", "1"))
    rows.tool("read", "c", call_id="r3", path="c.py")
    rows.tool("bash", "t again", call_id="s3", command="vibe skill load t", skill=("t", "2"))
    view = context_view(rows.rows)
    cut = 7  # before the read of c.py: the cut splits the parser turn
    assert carried_skills(view, cut) == (SkillRef("s", "1"),)
    first = compaction_payload(
        view,
        cut,
        mode="normal",
        reason="threshold",
        focus=None,
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
        focus=None,
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
    assert second["files_modified"] == ["b.py", "c.py"]
    assert second["current_request"] == first["current_request"]
    assert second["skills"] == [{"name": "s", "revision": "1"}, {"name": "t", "revision": "2"}]
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
