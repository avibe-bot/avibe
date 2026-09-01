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
        "test_reset_and_archive_events_preserve_barriers"
    )

    test_source = (ROOT / "tests/test_session_delivery_fsm.py").read_text()
    retired_tokens = (
        "test_session_lifecycle_waits_for_admitted_turn_capture",
        "final_" + "flush_memory_session",
        "MemoryWorker",
        "SessionFlushCoordinator",
    )
    assert all(token not in test_source for token in retired_tokens)


def test_memory_indep_021_catalog_points_to_executable_import_fence() -> None:
    catalog = yaml.safe_load(
        (ROOT / "tests/scenarios/memory_independence/catalog.yaml").read_text()
    )
    scenario = next(
        item for item in catalog["scenarios"] if item["id"] == "MEMORY-INDEP-021"
    )

    assert scenario["status"] == "covered"
    assert scenario["test"].endswith("test_memory_indep_021_status_import_fence")
    test_source = (ROOT / "tests/test_local_deps.py").read_text()
    assert "test_memory_indep_021_status_import_fence" in test_source


def test_memory_indep_027_catalog_points_to_preview_restart_convergence() -> None:
    catalog = yaml.safe_load(
        (ROOT / "tests/scenarios/memory_independence/catalog.yaml").read_text()
    )
    scenario = next(
        item for item in catalog["scenarios"] if item["id"] == "MEMORY-INDEP-027"
    )

    assert scenario["status"] == "covered"
    assert scenario["test"].endswith(
        "test_memory_indep_027_startup_retries_after_restart_admission"
    )
    test_source = (ROOT / "tests/test_ui_show_pages.py").read_text()
    assert "test_memory_indep_027_startup_retries_after_restart_admission" in test_source


def test_memory_indep_026_catalog_points_to_released_first_hop_upgrade() -> None:
    catalog = yaml.safe_load(
        (ROOT / "tests/scenarios/memory_independence/catalog.yaml").read_text()
    )
    scenario = next(
        item for item in catalog["scenarios"] if item["id"] == "MEMORY-INDEP-026"
    )

    assert scenario["status"] == "covered"
    assert scenario["test"].endswith(
        "test_memory_indep_026_upgrade_command_bridges_released_3_0_13_generation"
    )
    test_source = (ROOT / "tests/e2e/test_upgrade_command.py").read_text()
    assert "INITIAL_RELEASE_VERSION = \"3.0.13\"" in test_source
    assert "version('avibe-memory')" in test_source
