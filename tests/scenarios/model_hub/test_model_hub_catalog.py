"""Mechanical integrity checks for the Model Hub scenario catalog."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


SCENARIO_ROOT = Path("tests/scenarios/model_hub")


def _document(name: str) -> dict:
    return yaml.safe_load((SCENARIO_ROOT / name).read_text())


def _test_function(path: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(), filename=str(path))
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        ),
        None,
    )
    assert function is not None, f"Catalog points to missing test {path}::{function_name}"
    return function


def _decorator_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in function.decorator_list:
        node = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_mh_catalog_001_scenario_catalog_is_complete_and_executable() -> None:
    """MH-CATALOG-001: every live row resolves and every observation names a row."""

    catalog = _document("catalog.yaml")
    observations = _document("observations.yaml")
    scenarios = catalog["scenarios"]
    scenario_by_id = {item["id"]: item for item in scenarios}
    statuses = catalog["status_legend"]

    assert len(scenario_by_id) == len(scenarios)
    assert set(statuses) >= {item["status"] for item in scenarios}
    assert set(catalog["next_priority"]) == {item["id"] for item in scenarios if statuses[item["status"]]["priority"]}

    canonical_tests = set(catalog["capability"]["canonical_tests"])
    for scenario in scenarios:
        test_ref = scenario.get("test")
        status = statuses[scenario["status"]]
        if not status["test_required"]:
            assert test_ref is None
            continue
        assert isinstance(test_ref, str), f"Scenario {scenario['id']} has no executable test"
        path_text, function_name = test_ref.split("::", 1)
        path = Path(path_text)
        assert path_text in canonical_tests
        assert path.is_file()
        function = _test_function(path, function_name)
        expected_fail = "xfail" in _decorator_names(function)
        assert expected_fail == status["expected_fail"]

    observation_ids = [item["id"] for item in observations["observations"]]
    assert len(observation_ids) == len(set(observation_ids))
    assert all(
        scenario_id in scenario_by_id
        for observation in observations["observations"]
        for scenario_id in observation["affects"]
    )
