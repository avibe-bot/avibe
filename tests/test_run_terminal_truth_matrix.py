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
    test_path, *node_parts = node_id.split("::")
    assert test_path.startswith("tests/"), node_id
    assert node_parts, node_id
    test_name = node_parts[-1]
    tree = ast.parse(Path(test_path).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == test_name
        for node in ast.walk(tree)
    ), node_id


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


def test_the_q2_blocker_is_spelled_for_every_backend_and_lane() -> None:
    """HFR-186: Q2's per-backend/per-lane obligation has no missing cells."""
    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    assert PR7R_QUESTIONS["Q2"]["verdict"] == "blocked"
