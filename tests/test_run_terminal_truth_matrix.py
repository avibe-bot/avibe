"""HFR-184..HFR-186: the PR7R evidence matrix is closed and self-consistent.

Same contract as HFR-105 for ``TEARDOWN_SETTLEMENT_MATRIX``: growing a
dimension fails here until every new cell names a consuming test or a precise
ownership reason. PR7R adds two obligations HFR-105 does not have -- every
named node id must resolve to a real test function, and the ``unproven`` count
must equal the checked-in budget so a gap cannot widen or be papered over
silently.
"""

import ast
from pathlib import Path

import pytest

from tests.run_terminal_truth_evidence import (
    BACKENDS,
    EXACT_TURN_PROGRESS_SIGNALS,
    LANES,
    OUTCOMES,
    PR7R_FINDINGS,
    PR7R_QUESTIONS,
    RUN_TERMINAL_TRUTH_MATRIX,
    TRIGGERS,
    UNPROVEN_BUDGET,
)

_PROOF_KINDS = {"covered", "shared", "N/A", "defect", "unproven"}


def _expand() -> list[tuple[str, str, str, str, tuple[str, str]]]:
    """The full backend x lane x trigger x outcome product.

    A cell written once for the lane applies to every backend; the expansion is
    what makes that a claim about all three rather than a silent omission.
    """
    cells = []
    for backend in BACKENDS:
        for lane in LANES:
            for trigger in TRIGGERS:
                rows = RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)]
                for outcome in OUTCOMES:
                    cell = rows[outcome]
                    proof = cell.get("per_backend", {}).get(backend, cell["shared"])
                    cells.append((backend, lane, trigger, outcome, proof))
    return cells


_CELLS = _expand()


def _assert_node_exists(node_id: str) -> None:
    """Resolve the COMPLETE pytest node id, one nesting level at a time.

    Matching only the trailing function name and walking the whole module --
    which is what HFR-105 does, and what this guard did first -- accepts a node
    id whose class component is misspelled, renamed, or absent, as long as some
    same-named function exists anywhere in the file. For a matrix whose entire
    value is that its citations are real, that is the wrong failure mode: the
    citation would stay green while pointing at a test that no longer runs.
    """
    test_path, *node_parts = node_id.split("::")
    assert test_path.startswith("tests/"), node_id
    assert node_parts, node_id

    scope: list[ast.stmt] = ast.parse(
        Path(test_path).read_text(encoding="utf-8")
    ).body
    for depth, name in enumerate(node_parts):
        is_leaf = depth == len(node_parts) - 1
        wanted = (
            (ast.FunctionDef, ast.AsyncFunctionDef) if is_leaf else (ast.ClassDef,)
        )
        match = next(
            (
                node
                for node in scope
                if isinstance(node, wanted) and node.name == name
            ),
            None,
        )
        assert match is not None, (
            f"{node_id}: no {'test function' if is_leaf else 'class'} "
            f"named {name!r} at this level"
        )
        scope = match.body


def _detail_node(detail: str) -> str:
    """``shared``/``defect`` details carry ``<claim> -- <node id>``."""
    claim, marker, node_id = detail.rpartition(" -- ")
    assert marker, detail
    assert claim.strip(), detail
    return node_id.strip()


@pytest.mark.parametrize(
    ("backend", "lane", "trigger", "outcome", "proof"),
    _CELLS,
    ids=[f"{b}-{la}-{t}-{o}" for b, la, t, o, _p in _CELLS],
)
def test_every_run_terminal_truth_cell_is_proven_or_named_as_a_gap(
    backend: str,
    lane: str,
    trigger: str,
    outcome: str,
    proof: tuple[str, str],
) -> None:
    """HFR-184: every cell of the PR7R matrix carries an explicit proof."""
    assert set(RUN_TERMINAL_TRUTH_MATRIX) == {
        (lane_key, trigger_key) for lane_key in LANES for trigger_key in TRIGGERS
    }
    assert set(RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)]) == set(OUTCOMES)

    kind, detail = proof
    assert kind in _PROOF_KINDS, proof
    assert detail.strip(), proof

    if kind == "covered":
        _assert_node_exists(detail)
    elif kind in {"shared", "defect"}:
        _assert_node_exists(_detail_node(detail))
    elif kind == "unproven":
        # A gap is only useful if it says what would close it.
        assert "probe" in detail.lower(), proof
    else:  # N/A
        assert len(detail.split()) >= 4, proof


def test_the_unproven_count_matches_the_checked_in_budget() -> None:
    """HFR-185: the size of the gap is a number, not an impression.

    Lowering ``UNPROVEN_BUDGET`` requires writing the probe named in the cell;
    raising it requires saying so in the same commit. Either way the reviewer
    sees the delta instead of inferring it from prose.
    """
    unproven = [cell for cell in _CELLS if cell[4][0] == "unproven"]
    assert len(unproven) == UNPROVEN_BUDGET, sorted(
        f"{b}-{la}-{t}-{o}" for b, la, t, o, _p in unproven
    )
    # Sanity: the matrix is an evidence unit, not an empty shell.
    assert len(_CELLS) == len(BACKENDS) * len(LANES) * len(TRIGGERS) * len(OUTCOMES)
    assert UNPROVEN_BUDGET < len(_CELLS)


def test_every_question_and_finding_names_a_real_consuming_test() -> None:
    """HFR-186: the five plan questions and both findings are wired to tests."""
    assert set(PR7R_QUESTIONS) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
    for key, entry in PR7R_QUESTIONS.items():
        assert entry["verdict"] in {"answered", "open", "blocked"}, key
        assert entry["question"].strip() and entry["answer"].strip(), key
        assert entry["evidence"], key
        for node_id in entry["evidence"]:
            _assert_node_exists(node_id)

    for finding_id, finding in PR7R_FINDINGS.items():
        assert finding_id.startswith("PR7R-F"), finding_id
        assert finding["title"].strip() and finding["detail"].strip(), finding_id
        # The owner must be a real module path, so the contract amendment knows
        # who has to change.
        module_path = finding["owner"].split("::")[0]
        assert Path(module_path).exists(), finding_id
        _assert_node_exists(finding["reproducer"])

    # Every finding is reachable from the matrix, and every matrix defect cell
    # names a finding that exists. A defect recorded in only one of the two
    # places is how an evidence unit quietly loses a defect.
    referenced = {
        detail.split(" -- ")[0].strip()
        for _b, _la, _t, _o, (kind, detail) in _CELLS
        if kind == "defect"
    }
    assert referenced == set(PR7R_FINDINGS)


def test_a_citation_with_the_wrong_class_component_is_rejected() -> None:
    """HFR-186: the node resolver reads the whole id, not just its last word.

    The regression this pins: a class-qualified citation used to pass on the
    strength of the function name alone, so renaming the class -- or citing a
    class that never existed -- left the matrix pointing at a test pytest would
    not collect, with a green guard. Both real spellings are checked too, so
    the resolver cannot be "fixed" by rejecting everything.
    """
    real = (
        "tests/test_agent_stop_settlement.py::AgentStopSettlementTests::"
        "test_no_backend_stop_uses_the_terminal_turn_default"
    )
    _assert_node_exists(real)  # class-qualified, correct
    _assert_node_exists(  # module-level, correct
        "tests/test_run_terminal_truth_matrix.py::"
        "test_the_unproven_count_matches_the_checked_in_budget"
    )

    with pytest.raises(AssertionError, match="no class named"):
        _assert_node_exists(real.replace("AgentStopSettlementTests", "RenamedTests"))
    with pytest.raises(AssertionError, match="no test function named"):
        # The class is right; the function moved out from under it.
        _assert_node_exists(
            "tests/test_agent_stop_settlement.py::AgentStopSettlementTests::"
            "test_no_such_test_lives_in_this_class"
        )
    with pytest.raises(AssertionError, match="no class named"):
        # A real module-level function cited as if it were nested.
        _assert_node_exists(
            "tests/test_run_terminal_truth_matrix.py::NotAClass::"
            "test_the_unproven_count_matches_the_checked_in_budget"
        )


def test_the_q2_signal_table_is_spelled_for_every_backend_and_lane() -> None:
    """HFR-186: Q2's per-backend/per-lane obligation has no missing cells.

    The verdict is tied to the table rather than pinned to a literal. Q2 opened
    once a backend was shown to carry an exact-Turn attribution, and it may only
    close when EVERY cell does -- the plan's rule is all-or-nothing, so a partial
    table and an ``answered`` verdict cannot both be true.
    """
    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    kinds = {kind for kind, _reason in EXACT_TURN_PROGRESS_SIGNALS.values()}
    verdict = PR7R_QUESTIONS["Q2"]["verdict"]
    if kinds == {"unproven"}:
        assert verdict == "blocked", kinds
    elif kinds == {"covered"}:
        assert verdict == "answered", kinds
    else:
        assert verdict == "open", kinds
