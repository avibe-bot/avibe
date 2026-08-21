"""Mechanical contract for the active IM attachment capture scenarios."""

from pathlib import Path

import pytest
import yaml


SCENARIO_ROOT = Path("tests/scenarios/memory_im_attachment_capture")
PROJECT_INDEX = Path("tests/scenarios/INDEX.yaml")

LOCKED_CONTRACT = {
    "admission": "bound_enabled_one_to_one_dm_fail_closed",
    "opt_in": "explicit_complete_multimodal_endpoint",
    "degradation": "text_and_valid_attachments_survive_independently",
    "formats": "everos_parser_minus_svg_video_office_requires_live_soffice",
    "limits": "memory_only_8_files_25_mib_each_100_mib_bundle",
    "preflight": "generated_image_without_user_data",
    "workbench_compatibility": "implicit_main_llm_for_one_cycle",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_memory_im_attachment_catalog_is_indexed_and_locks_the_approved_contract() -> None:
    catalog = _load(SCENARIO_ROOT / "catalog.yaml")
    observations = _load(SCENARIO_ROOT / "observations.yaml")
    index = _load(PROJECT_INDEX)
    entry = next(
        item
        for item in index["capabilities"]
        if item["id"] == "memory_im_attachment_capture"
    )

    assert catalog["contract"] == LOCKED_CONTRACT
    assert entry["status"] == "active"
    assert entry["catalog"] == (SCENARIO_ROOT / "catalog.yaml").as_posix()
    assert entry["observations"] == (SCENARIO_ROOT / "observations.yaml").as_posix()
    assert entry["scenario_tests"] == [
        (SCENARIO_ROOT / "test_memory_im_attachment_capture_scenarios.py").as_posix()
    ]
    assert entry["harness"] == [
        "tests/scenario_harness/memory_im_attachments.py"
    ]

    scenario_ids = [row["id"] for row in catalog["scenarios"]]
    assert len(scenario_ids) == len(set(scenario_ids))
    assert all(
        scenario_id in scenario_ids
        for observation in observations["observations"]
        for scenario_id in observation["scenarios"]
    )


@pytest.mark.parametrize(
    ("scenario_id", "kind", "delivery_slice"),
    [
        ("MEMORY-IM-ATTACH-001", "happy_path", 4),
        ("MEMORY-IM-ATTACH-002", "authorization", 4),
        ("MEMORY-IM-ATTACH-003", "degradation", 4),
        ("MEMORY-IM-ATTACH-004", "boundary", 4),
        ("MEMORY-IM-ATTACH-005", "platform_contract", 5),
        ("MEMORY-IM-ATTACH-006", "platform_contract", 5),
        ("MEMORY-IM-ATTACH-007", "platform_contract", 5),
        ("MEMORY-IM-ATTACH-008", "platform_contract", 5),
        ("MEMORY-IM-ATTACH-009", "degradation", 6),
        ("MEMORY-IM-ATTACH-010", "boundary", 4),
        ("MEMORY-IM-ATTACH-011", "degradation", 7),
        ("MEMORY-IM-ATTACH-012", "boundary", 8),
    ],
)
def test_memory_im_attachment_covered_scenario_contract(
    scenario_id: str,
    kind: str,
    delivery_slice: int,
) -> None:
    """Every MEMORY-IM-ATTACH scenario ID maps to executable evidence."""

    catalog = _load(SCENARIO_ROOT / "catalog.yaml")
    rows = {row["id"]: row for row in catalog["scenarios"]}

    assert len(rows) == len(catalog["scenarios"])
    assert set(rows) == {
        "MEMORY-IM-ATTACH-001",
        "MEMORY-IM-ATTACH-002",
        "MEMORY-IM-ATTACH-003",
        "MEMORY-IM-ATTACH-004",
        "MEMORY-IM-ATTACH-005",
        "MEMORY-IM-ATTACH-006",
        "MEMORY-IM-ATTACH-007",
        "MEMORY-IM-ATTACH-008",
        "MEMORY-IM-ATTACH-009",
        "MEMORY-IM-ATTACH-010",
        "MEMORY-IM-ATTACH-011",
        "MEMORY-IM-ATTACH-012",
    }
    assert rows[scenario_id]["status"] == "covered"
    assert rows[scenario_id]["kind"] == kind
    assert rows[scenario_id]["layer"] == "scenario"
    assert rows[scenario_id]["delivery_slice"] == delivery_slice
    assert rows[scenario_id]["test"].startswith(
        "tests/scenarios/memory_im_attachment_capture/"
    )
