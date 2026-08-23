"""Mechanical guardrails for the accepted-loss Memory lifecycle contract."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


def test_retained_memory_lifecycle_scenarios_do_not_depend_on_retired_delivery_api() -> None:
    catalog = yaml.safe_load(
        (ROOT / "tests/scenarios/memory_independence/catalog.yaml").read_text()
    )
    scenario = next(
        item for item in catalog["scenarios"] if item["id"] == "MEMORY-INDEP-010"
    )
    assert "non-blocking" in scenario["name"].lower()
    assert scenario["test"].endswith(
        "test_session_lifecycle_barrier_is_non_blocking_for_admitted_turn_capture"
    )

    test_source = (ROOT / "tests/test_session_delivery_fsm.py").read_text()
    retired_tokens = (
        "test_session_lifecycle_waits_for_admitted_turn_capture",
        "final_flush_memory_session",
        "MemoryWorker",
        "SessionFlushCoordinator",
    )
    assert all(token not in test_source for token in retired_tokens)
