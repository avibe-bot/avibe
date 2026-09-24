"""Mechanical evidence binding for Memory retirement upgrade scenarios."""

import ast
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
SCENARIO_ROOT = ROOT / "tests/scenarios/memory_upgrade_compatibility"


def test_memory_upgrade_scenario_ids_resolve_to_executable_evidence():
    index = yaml.safe_load((ROOT / "tests/scenarios/INDEX.yaml").read_text())
    entry, = [item for item in index["capabilities"] if item["id"] == "memory_upgrade_compatibility"]
    catalog = yaml.safe_load((SCENARIO_ROOT / "catalog.yaml").read_text())
    observations = yaml.safe_load((SCENARIO_ROOT / "observations.yaml").read_text())
    assert {entry["catalog"], entry["observations"]} == {
        "tests/scenarios/memory_upgrade_compatibility/catalog.yaml",
        "tests/scenarios/memory_upgrade_compatibility/observations.yaml",
    }
    scenarios = catalog["scenarios"]
    ids = {item["id"] for item in scenarios}
    assert len(ids) == len(scenarios)
    for scenario in scenarios:
        path, name = scenario["test"].split("::")
        assert path in catalog["capability"]["canonical_tests"]
        assert path in entry["scenario_tests"] or path in entry["unit_or_contract_tests"]
        functions = [node for node in ast.parse((ROOT / path).read_text()).body if isinstance(node, ast.FunctionDef)]
        function, = [node for node in functions if node.name == name]
        assert scenario["id"] in (ast.get_docstring(function) or "")
    for observation in observations["observations"]:
        assert set(observation["affects"]) <= ids
