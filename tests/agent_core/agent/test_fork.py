"""C-10 fork point as pure functions (``agent-core-contracts/fork.md`` section 2)."""

import pytest

from core.agent_core.harness.fork import ForkPoint, ForkPointError, fork_point, fork_prefix, latest_cut, settled
from core.agent_core.harness.projection import project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from tests.agent_core.agent.test_projection import _checkpoint_payload
from tests.agent_core.fakes import assistant, user


class Rows:
    def __init__(self) -> None:
        self.rows: list[ContextEntry] = []

    def add(self, kind, message=None, payload=None) -> ContextEntry:
        seq = len(self.rows) + 1
        row = ContextEntry("source", seq, kind, f"{kind}-{seq}", message, payload or {})
        self.rows.append(row)
        return row

    def input(self, value: str) -> ContextEntry:
        return self.add("input", user(value))

    def reply(self, value: str) -> ContextEntry:
        return self.add("response", assistant(value))

    def calls(self, *ids: str) -> ContextEntry:
        return self.add("response", assistant("working", calls=[ToolCallBlock(call, "read") for call in ids]))

    def result(self, call: str, value: str = "ok") -> ContextEntry:
        return self.add("tool_result", ToolResultMessage(call, "read", (text(value),)))


def _ended_turn_then(rows: Rows) -> int:
    rows.input("first")
    return rows.reply("first answer").context_seq


def test_a_users_fork_of_a_turn_mid_tool_batch_cuts_at_the_previous_ended_turn():
    rows = Rows()
    ended = _ended_turn_then(rows)
    live = rows.input("second")
    rows.calls("a")
    rows.result("a")
    rows.calls("b", "c")
    rows.result("b")  # c is still running
    assert latest_cut(rows.rows, live_input_seq=live.context_seq) == ended
    # Without the live Turn's first input the largest settled point is mid-Turn: what a self-fork takes.
    assert latest_cut(rows.rows, live_input_seq=None) == 5


@pytest.mark.parametrize("outcome", ["stopped", "failed"])
def test_a_stopped_or_failed_previous_turn_is_a_legal_cut_once_recovery_settled_it(outcome):
    rows = Rows()
    _ended_turn_then(rows)
    rows.input("second")
    rows.calls("a", "b")
    rows.result("a")
    # Recovery (T2) settles what the Stop or the failure left open, before the next Turn's input.
    settled_by_recovery = rows.result("b", f"[tool call interrupted: {outcome}]")
    live = rows.input("third")
    assert latest_cut(rows.rows, live_input_seq=live.context_seq) == settled_by_recovery.context_seq
    assert latest_cut(rows.rows, live_input_seq=None) == live.context_seq


def test_an_ended_turn_recovery_has_not_settled_contributes_up_to_its_last_settled_row():
    rows = Rows()
    _ended_turn_then(rows)
    ask = rows.input("second")
    rows.calls("a")  # the Turn ended by a crash; T2 has not run yet
    assert latest_cut(rows.rows, live_input_seq=None) == ask.context_seq


def test_an_empty_or_unanswered_context_cuts_at_the_empty_prefix():
    assert latest_cut([], live_input_seq=None) == 0
    rows = Rows()
    live = rows.input("only")
    assert latest_cut(rows.rows, live_input_seq=live.context_seq) == 0


def test_the_internal_point_must_be_in_range_and_settled():
    rows = Rows()
    _ended_turn_then(rows)
    rows.calls("a")
    rows.result("a")
    assert fork_point(rows.rows, 0) == ForkPoint(0)
    assert fork_point(rows.rows, 4) == ForkPoint(4)
    with pytest.raises(ForkPointError) as unsettled:
        fork_point(rows.rows, 3)
    assert unsettled.value.code == "unsettled"
    for outside in (-1, 5):
        with pytest.raises(ForkPointError) as out_of_range:
            fork_point(rows.rows, outside)
        assert out_of_range.value.code == "out_of_range"
    assert settled(rows.rows, 2) and not settled(rows.rows, 3)


def test_the_prefix_is_the_view_at_the_cut_and_a_later_checkpoint_does_not_exist_there():
    rows = Rows()
    _ended_turn_then(rows)
    rows.input("second")
    rows.reply("second answer")
    rows.add("compaction", payload=_checkpoint_payload(3))
    assert fork_prefix(rows.rows, ForkPoint(4)) == project(rows.rows, fork_point=4).messages
    assert [message.content[0].text for message in fork_prefix(rows.rows, ForkPoint(4))] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]
    assert fork_prefix(rows.rows, ForkPoint(4, units=2)) == project(rows.rows, fork_point=2).messages
