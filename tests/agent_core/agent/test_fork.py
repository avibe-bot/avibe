"""C-10 fork: the fork point as pure functions (``agent-core-contracts/fork.md`` section 2), and the side turn
(sections 7 and 15)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft7Validator
from referencing import Registry, Resource

from core.agent_core.agent.fork import DREAMING, CheckpointDetail, Folded, SideTurn
from core.agent_core.agent.models import ModelSelection
from core.agent_core.ai.provider import Done
from core.agent_core.harness.fork import ForkPoint, ForkPointError, fork_point, fork_prefix, latest_cut, settled
from core.agent_core.harness.projection import context_view, project
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from tests.agent_core.agent.test_compaction import (
    CHECKPOINT,
    ROOMY,
    SELECTION,
    Model,
    after_checkpoint,
    call,
    cross,
    history,
    make_agent,
    reader,
    run,
    tokens,
)
from tests.agent_core.agent.test_projection import _checkpoint_payload
from tests.agent_core.fakes import ENDPOINT, FakeTool, InMemoryTranscriptStore, assistant, user


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
    assert latest_cut(rows.rows, unended_input_seq=live.context_seq) == ended
    # Without the live Turn's first input the largest settled point is mid-Turn: what a self-fork takes.
    assert latest_cut(rows.rows, unended_input_seq=None) == 5


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
    assert latest_cut(rows.rows, unended_input_seq=live.context_seq) == settled_by_recovery.context_seq
    assert latest_cut(rows.rows, unended_input_seq=None) == live.context_seq


def test_an_ended_turn_recovery_has_not_settled_is_cut_out_whole():
    rows = Rows()
    ended = _ended_turn_then(rows)
    ask = rows.input("second")
    rows.calls("a")
    rows.result("a")
    rows.calls("b")  # the Turn ended by a crash; T2 has not settled b yet
    # The storage layer names that Turn's first input; nothing of the partial Turn is kept.
    assert latest_cut(rows.rows, unended_input_seq=ask.context_seq) == ended
    # Without it, the largest settled point would fall mid-Turn: what a self-fork from inside a live Turn takes.
    assert latest_cut(rows.rows, unended_input_seq=None) == 5


def test_an_empty_or_unanswered_context_cuts_at_the_empty_prefix():
    assert latest_cut([], unended_input_seq=None) == 0
    rows = Rows()
    live = rows.input("only")
    assert latest_cut(rows.rows, unended_input_seq=live.context_seq) == 0


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


# --- the side turn (fork.md sections 7 and 15) ------------------------------------------------------------------


def _side_turn(**fields) -> SideTurn:
    async def fold(reply):
        return Folded(None)

    defaults = dict(
        purpose="checkpoint",
        detail=CheckpointDetail("threshold", "normal"),
        units=None,
        prompt=user("checkpoint"),
        policy=DREAMING,
        max_tokens=lambda capabilities: 1_000,
        fold=fold,
    )
    return SideTurn(**{**defaults, **fields})


def test_a_side_turn_is_built_only_with_its_purposes_detail():
    # Every fork_turn audit records its detail, so a side turn cannot be built without the one its purpose needs.
    assert _side_turn().detail == CheckpointDetail("threshold", "normal")
    with pytest.raises(TypeError):
        _side_turn(detail=None)
    with pytest.raises(TypeError):
        _side_turn(detail={"reason": "threshold", "mode": "normal"})
    with pytest.raises(ValueError):
        _side_turn(purpose="memory")
    with pytest.raises(TypeError):
        SideTurn("checkpoint", units=None, prompt=user("p"), policy=DREAMING, max_tokens=len, fold=None)


def _fork_turn_validator() -> Draft7Validator:
    contracts = Path(__file__).resolve().parents[3] / "docs" / "plans" / "agent-core-contracts"
    resources = {}
    for path in contracts.glob("*.schema.json"):
        value = json.loads(path.read_text())
        resources[value["$id"]] = Resource.from_contents(value)
    return Draft7Validator(
        {"$ref": "agent-core/transcript-rows.schema.json#/definitions/ForkTurn"},
        registry=Registry().with_resources(resources.items()),
    )


async def test_every_side_turn_leaves_one_fork_turn_audit_with_its_purpose_policy_point_and_detail():
    # F7 and section 15, on C-9's overflow-then-roll case: a normal checkpoint over the whole view reads once, then
    # its next request cannot fit a smaller route and fails; a rolling one over a prefix of the view completes.
    small = ModelSelection(ENDPOINT, replace(SELECTION.capabilities, context_window=8_000, input_limit=None))
    store = InMemoryTranscriptStore()
    await run(make_agent(Model(history()), store=store, tools=[reader(tokens(3_000))], selection=ROOMY))
    model = Model([[Done(assistant("after"))]], [call("read", "first", path="f"), [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, store=store, tools=[reader(tokens(3_000))], selection=(ROOMY, small))
    await cross(agent)

    rows = await store.load("session")
    compaction = next(row for row in rows if row.kind == "compaction")
    before = [row for row in rows if row.context_seq < compaction.context_seq]
    latest = before[-1].context_seq  # the caller's latest point when both turns ran: neither wrote context
    normal, rolling = (audit for _, _, audit in store.audits)
    validator = _fork_turn_validator()
    validator.validate(normal)
    validator.validate(rolling)
    assert {key: normal[key] for key in ("purpose", "policy", "point", "detail", "rounds", "outcome")} == {
        "purpose": "checkpoint",
        "policy": "dreaming",
        "point": {"as_of": latest, "units": None},
        "detail": {"reason": "threshold", "mode": "normal"},
        "rounds": 1,
        "outcome": "failed",
    }
    assert normal["folded_event_id"] is None and normal["error"].startswith("overflow: the checkpoint request")
    assert rolling["detail"] == {"reason": "overflow", "mode": "rolling"}
    assert rolling["point"]["as_of"] == latest and 0 < rolling["point"]["units"] < len(context_view(before).units)
    assert (rolling["outcome"], rolling["folded_event_id"], rolling["error"]) == ("completed", compaction.row_id, None)


async def test_a_side_turn_call_has_no_instance_even_when_its_policy_allows_the_tool(monkeypatch):
    # F4: a side turn starts nothing. Its calls have no committed response, so no call instance, whatever the
    # policy lets run.
    monkeypatch.setattr(
        "core.agent_core.agent.loop.DREAMING", replace(DREAMING, tools={**DREAMING.tools, "bash": "allow"})
    )
    bash = FakeTool("bash")
    model = Model(after_checkpoint(), [call("bash", "side", command="sleep 600"), [Done(assistant(CHECKPOINT))]])
    agent = make_agent(model, tools=[reader(tokens(3_000)), bash], selection=ROOMY)
    await run(agent)
    assert (await cross(agent))[-1].reason == "completed"

    ((arguments, ctx),) = bash.calls
    assert arguments == {"command": "sleep 600"} and ctx.call is None
