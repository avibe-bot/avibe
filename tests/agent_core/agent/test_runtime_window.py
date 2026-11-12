"""Runtime-window owner contracts: pruning must not change the existing oracles.

Historical projection tests exercise complete inputs. These tests specifically
remove older rows and then compare projection, anchors/budgets, recovery, and the
next checkpoint's memo/skill inputs against those unchanged implementations.
"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.agent_core.agent.loop import Agent
from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.harness.context import (
    ContextConfig,
    budget,
    last_anchor,
    skill_candidates,
)
from core.agent_core.harness.projection import ProjectionError, context_view, open_tool_calls, project
from core.agent_core.harness.runtime import compact_runtime, is_anchor_response
from core.agent_core.messages import Origin, ToolCallBlock, Usage
from tests.agent_core.agent.test_context import CAPABILITIES, Rows, make_request
from tests.agent_core.agent.test_projection import _checkpoint_payload, _checkpointed_rows, _edit
from tests.agent_core.fakes import assistant


def checkpoint(rows, first_kept, *, legacy=False, **fields):
    carried = {} if legacy else {"memo": [], "skills_carried": []}
    return rows.add("compaction", payload=_checkpoint_payload(first_kept, **carried, **fields))


def anchor(rows, value="anchored", *, usage=None, **fields):
    message = replace(assistant(value), usage=usage or Usage(input_tokens=900, output_tokens=10), **fields)
    return rows.add("response", message, payload={"request": {"tokens": 100}})


def memo_lists(rows):
    """Use the actual loop's memo selection with fixture inputs typed by people."""
    agent = object.__new__(Agent)
    agent.context = ContextConfig(host=SimpleNamespace())
    agent._rows = list(rows)
    agent._typed = {row.row_id: f"typed #{row.context_seq}" for row in rows if row.kind == "input"}
    agent._typed_failure = None
    agent._turn_inputs = set()
    view = context_view(rows)
    return [agent._memo_list(view, cut) for cut in range(view.pinned, len(view.units))]


def assert_runtime_equal(full, retained):
    assert project(retained) == project(full)
    expected, actual = context_view(full), context_view(retained)
    assert actual.units == expected.units
    assert actual.pinned == expected.pinned
    assert last_anchor(retained, actual) == last_anchor(full, expected)
    request = make_request(expected.messages)
    assert budget(request, CAPABILITIES, transcript=actual.messages, anchor=last_anchor(retained, actual)) == budget(
        request, CAPABILITIES, transcript=expected.messages, anchor=last_anchor(full, expected)
    )
    assert open_tool_calls(retained) == open_tool_calls(full)
    assert memo_lists(retained) == memo_lists(full)
    for cut in range(expected.pinned, len(expected.units)):
        assert skill_candidates(actual, cut) == skill_candidates(expected, cut)


def test_postcheckpoint_anchor_prunes_history_but_preserves_state_refusals_and_current_context():
    rows = Rows()
    rows.input("old request")
    state = rows.add("agent_state", payload={"version": 1, "state": {"counter": [2]}})
    refusal = rows.add("replay_refusal", payload={"version": 1, "source_identity": "src", "payloads": ["signature"]})
    for index in range(20):
        rows.response(value=f"old output {index}")
    current = rows.input("current")
    checkpoint(rows, current.context_seq)
    anchor(rows)
    original = deepcopy(rows.rows)
    result = compact_runtime(rows.rows)
    assert_runtime_equal(rows.rows, result.entries)
    assert len(result.entries) == 5
    assert state in result.entries and refusal in result.entries
    assert rows.rows == original
    assert all(any(entry is original_row for original_row in rows.rows) for entry in result.entries)


@pytest.mark.parametrize("has_previous_checkpoint", [False, True])
def test_precheckpoint_anchor_retains_its_original_prefix_and_all_asof_edit_versions(has_previous_checkpoint):
    rows = Rows()
    rows.input("obsolete")
    rows.response(value="obsolete reply")
    current = rows.input("older active request")
    if has_previous_checkpoint:
        checkpoint(rows, current.context_seq)
    call = ToolCallBlock("a", "read", {"path": "a.py"})
    rows.response(call)
    result = rows.result(call, "original result", skills=("skill-a",))
    rows.rows.append(_edit(len(rows.rows) + 1, "before-anchor", result.row_id, "first replacement"))
    kept = anchor(rows)
    rows.rows.append(_edit(len(rows.rows) + 1, "after-anchor", result.row_id, "latest replacement"))
    checkpoint(rows, kept.context_seq)
    reduced = compact_runtime(rows.rows)
    assert_runtime_equal(rows.rows, reduced.entries)
    assert result in reduced.entries
    assert {"before-anchor", "after-anchor"} <= {row.row_id for row in reduced.entries}
    assert (rows.rows[0] in reduced.entries) is (not has_previous_checkpoint)


def test_base_reference_set_becomes_exact_after_new_suffix_anchor_without_reloading_first_checkpoint_history():
    rows = Rows()
    for index in range(25):
        rows.input(f"old input {index}")
        rows.response(value=f"old output {index}")
    kept = anchor(rows)
    checkpoint(rows, kept.context_seq)
    full_capsule = compact_runtime(rows.rows)
    base = compact_runtime(rows.rows, preserve_anchor=False)
    assert len(full_capsule.entries) == len(rows.rows)
    assert len(base.entries) == 2
    new = anchor(rows, "new anchor")
    assert is_anchor_response(new)
    result = compact_runtime((*base.entries, new), prior=base)
    assert_runtime_equal(rows.rows, result.entries)
    checkpoint(rows, new.context_seq)
    next_capsule = compact_runtime((*result.entries, rows.rows[-1]), prior=result)
    assert_runtime_equal(rows.rows, next_capsule.entries)
    assert len(next_capsule.entries) < 6


def test_late_result_keeps_summarized_owner_complete_batch_and_unsettled_calls():
    rows = Rows()
    rows.rows = _checkpointed_rows()
    rows.rows[8] = replace(rows.rows[8], payload={**rows.rows[8].payload, "memo": [], "skills_carried": []})
    # Existing projection fixture: owner seq2 precedes F=3, result arrives at seq5.
    reduced = compact_runtime(rows.rows)
    assert_runtime_equal(rows.rows, reduced.entries)
    assert rows.rows[1] in reduced.entries
    assert rows.rows[4] in reduced.entries
    # A checkpoint does not remove a pending call from recovery's responsibility.
    pending = Rows()
    pending.input("older")
    a, b = ToolCallBlock("a", "bash"), ToolCallBlock("b", "read")
    owner = pending.response(a, b)
    settled = pending.result(a, "finished sibling")
    current = pending.input("new request")
    checkpoint(pending, current.context_seq)
    capsule = compact_runtime(pending.rows)
    assert owner in capsule.entries and settled in capsule.entries
    assert_runtime_equal(pending.rows, capsule.entries)
    late = pending.result(b, "recovered")
    restored = compact_runtime((*capsule.entries, late), prior=capsule)
    assert_runtime_equal(pending.rows, restored.entries)


def test_reused_call_ids_close_only_the_retained_instance_and_keep_its_edit_history():
    rows = Rows()
    rows.input("old")
    call = ToolCallBlock("same-id", "read")
    old_owner = rows.response(call)
    old_result = rows.result(call, "old output")
    rows.rows.append(_edit(len(rows.rows) + 1, "old-edit", old_result.row_id, "old cleared"))
    current = rows.input("current")
    new_owner = rows.response(call)
    new_result = rows.result(call, "new output", skills=("skill-new",))
    checkpoint(rows, current.context_seq)
    anchor(rows)
    reduced = compact_runtime(rows.rows)
    assert_runtime_equal(rows.rows, reduced.entries)
    assert old_owner not in reduced.entries and old_result not in reduced.entries
    assert "old-edit" not in {row.row_id for row in reduced.entries}
    assert new_owner in reduced.entries and new_result in reduced.entries
    assert reduced.change_seqs["context_edit"] == 4


@pytest.mark.parametrize("legacy", [False, True])
def test_memo_retains_legacy_inputs_or_unsummarized_inputs_before_first_kept_and_released_pins(legacy):
    rows = Rows()
    oldest = rows.input("old constraint")
    rows.response(value="old reply")
    unsummarized = rows.input("not yet offered")
    pinned = rows.input("pinned request")
    current = rows.response(value="retained reply")
    checkpoint(rows, current.context_seq, legacy=legacy, summarized_to_seq=2, kept_inputs=[pinned.context_seq])
    anchor(rows)
    result = compact_runtime(rows.rows)
    assert_runtime_equal(rows.rows, result.entries)
    assert unsummarized in result.entries and pinned in result.entries
    assert (oldest in result.entries) is legacy
    # A later v5 checkpoint no longer needs the legacy-only input references.
    later = checkpoint(rows, current.context_seq, summarized_to_seq=4, kept_inputs=[pinned.context_seq])
    next_result = compact_runtime((*result.entries, later), prior=result)
    assert_runtime_equal(rows.rows, next_result.entries)
    assert oldest not in next_result.entries


def cache_record(*, started_at=10.0, marker="first", input_tokens=100):
    return {
        "version": 1, "provider": "test-provider", "model": "test-model", "route": "route",
        "started_at": started_at, "context_seq": 0, "prefix": [["hash", 10, 0]],
        "input_tokens": input_tokens, "marker": marker,
    }


def test_prior_scalar_facts_survive_pruning_and_cache_ties_follow_canonical_response_order():
    rows = Rows()
    rows.input("old")
    old = rows.response(value="old route reply")
    rows.rows[-1] = replace(old, created_at=100.0, payload={"cache": cache_record(marker="older")})
    winner = rows.response(value="later equal timestamp")
    rows.rows[-1] = replace(winner, created_at=90.0, payload={"cache": cache_record(marker="winner")})
    current = rows.input("current")
    checkpoint(rows, current.context_seq)
    first = compact_runtime(rows.rows)
    assert first.response_clocks == {("test-provider", "test-model"): 100.0}
    assert first.cache_records[0]["marker"] == "winner"
    # An old retained response with the same start clock cannot replace the
    # already-certified later winner when an incremental capsule is derived.
    retained = replace(first, entries=(rows.rows[1], *first.entries))
    failed = anchor(rows, "failed", stop_reason="error")
    failed = replace(failed, created_at=120.0, message=replace(failed.message, origin=Origin("other", "anthropic", "m")))
    rows.rows[-1] = failed
    assert not is_anchor_response(failed)
    checkpoint(rows, current.context_seq)
    incremental = compact_runtime((*retained.entries, failed, rows.rows[-1]), prior=retained)
    assert_runtime_equal(rows.rows, incremental.entries)
    assert incremental.response_clocks == {("test-provider", "test-model"): 100.0, ("other", "m"): 120.0}
    assert incremental.cache_records[0]["marker"] == "winner"
    tracker = CacheTracker()
    tracker.restore(rows.rows, ())
    assert incremental.cache_records == tuple(tracker.latest[key] for key in sorted(tracker.latest))
    new = anchor(rows, "new successful reply")
    rows.rows[-1] = replace(new, created_at=130.0, payload={**new.payload, "cache": cache_record(marker="new")})
    final = compact_runtime((*incremental.entries, rows.rows[-1]), prior=incremental)
    assert final.cache_records[0]["marker"] == "new"
    assert final.change_seqs["compaction"] == rows.rows[-2].context_seq


def test_final_edit_keeps_true_head_and_dependencies_even_when_its_result_is_summarized():
    rows = Rows()
    rows.input("old")
    call = ToolCallBlock("a", "read")
    owner = rows.response(call)
    result = rows.result(call, "original")
    current = rows.input("current")
    checkpoint(rows, current.context_seq)
    rows.rows.append(_edit(len(rows.rows) + 1, "final-edit", result.row_id, "replaced"))
    reduced = compact_runtime(rows.rows)
    assert reduced.entries[-1] is rows.rows[-1]
    assert owner in reduced.entries and result in reduced.entries
    assert_runtime_equal(rows.rows, reduced.entries)


def test_validation_precedes_pruning_and_missing_cross_boundary_target_requests_canonical_rehydration():
    rows = Rows()
    rows.input("old")
    call = ToolCallBlock("a", "read")
    rows.response(call)
    result = rows.result(call, "original")
    current = rows.input("current")
    checkpoint(rows, current.context_seq)
    reduced = compact_runtime(rows.rows)
    edit = _edit(len(rows.rows) + 1, "late-edit", result.row_id, "replacement")
    with pytest.raises(ProjectionError, match="targets no earlier tool result"):
        compact_runtime((*reduced.entries, edit), prior=reduced)
    assert_runtime_equal((*rows.rows, edit), compact_runtime((*rows.rows, edit)).entries)
    corrupt = _edit(len(rows.rows) + 1, "bad-edit", "missing", "replacement")
    rows.rows.append(corrupt)
    checkpoint(rows, current.context_seq)
    with pytest.raises(ProjectionError, match="targets no earlier tool result"):
        compact_runtime(rows.rows)


@pytest.mark.parametrize("dependency", ["earlier_cut", "earlier_memo_boundary", "legacy_memo"])
def test_incremental_checkpoint_cannot_silently_resurrect_an_omitted_prefix(dependency):
    rows = Rows()
    old = rows.input("old user's words")
    rows.response(value="old reply")
    current = rows.input("current")
    checkpoint(rows, current.context_seq)
    prior = compact_runtime(rows.rows)
    assert old not in prior.entries
    fields = {"summarized_to_seq": 0} if dependency == "earlier_memo_boundary" else {}
    newer = checkpoint(
        rows,
        old.context_seq if dependency == "earlier_cut" else current.context_seq,
        legacy=dependency == "legacy_memo",
        **fields,
    )
    # Canonical history is valid: fallback may rebuild it, not reject the turn.
    assert_runtime_equal(rows.rows, compact_runtime(rows.rows).entries)
    with pytest.raises(ProjectionError, match="canonical prefix rehydration"):
        compact_runtime((*prior.entries, newer), prior=prior)
