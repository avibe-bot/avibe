"""Small executable guard for PR7R's conservative evidence baseline."""

import pytest

from tests.run_terminal_truth_evidence import (
    BACKENDS,
    EXACT_TURN_PROGRESS_SIGNALS,
    LANES,
    OUTCOMES,
    PR7R_QUESTIONS,
    RUN_TERMINAL_TRUTH_MATRIX,
    TRIGGERS,
    UNPROVEN_BUDGET,
)


def _cells():
    for backend in BACKENDS:
        for lane in LANES:
            for trigger in TRIGGERS:
                for outcome, proof in RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)].items():
                    yield backend, lane, trigger, outcome, proof


@pytest.mark.parametrize(
    ("backend", "lane", "trigger", "outcome", "proof"),
    tuple(_cells()),
)
def test_every_pr7r_cell_names_the_probe_that_would_close_it(
    backend: str,
    lane: str,
    trigger: str,
    outcome: str,
    proof: tuple[str, str],
) -> None:
    """HFR-180: every dimension combination remains explicit."""
    assert set(RUN_TERMINAL_TRUTH_MATRIX) == {
        (known_lane, known_trigger)
        for known_lane in LANES
        for known_trigger in TRIGGERS
    }
    assert set(RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)]) == set(OUTCOMES)
    assert proof[0] == "unproven", (backend, lane, trigger, outcome, proof)
    assert proof[1].startswith("Probe:"), proof


def test_pr7r_gap_budget_matches_the_full_product() -> None:
    """HFR-181: the baseline cannot imply coverage by dropping cells."""
    cells = tuple(_cells())
    assert len(cells) == len(BACKENDS) * len(LANES) * len(TRIGGERS) * len(OUTCOMES)
    assert sum(proof[0] == "unproven" for *_, proof in cells) == UNPROVEN_BUDGET


def test_q2_remains_blocked_until_every_backend_lane_has_exact_attribution() -> None:
    """HFR-182: all six exact-attribution obligations stay visible."""
    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    assert all(proof[0] == "unproven" for proof in EXACT_TURN_PROGRESS_SIGNALS.values())
    assert PR7R_QUESTIONS["Q2"]["verdict"] == "blocked"
