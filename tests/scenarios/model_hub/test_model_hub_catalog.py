"""Mechanical integrity checks for the Model Hub scenario catalog."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


SCENARIO_ROOT = Path("tests/scenarios/model_hub")
PROJECT_INDEX = Path("tests/scenarios/INDEX.yaml")
CAPABILITY_ID = "model_hub"


def _document(name: str) -> dict:
    return yaml.safe_load((SCENARIO_ROOT / name).read_text())


def _indexed_capability() -> dict:
    index = yaml.safe_load(PROJECT_INDEX.read_text())
    entry = next((item for item in index["capabilities"] if item["id"] == CAPABILITY_ID), None)
    assert entry is not None, f"{PROJECT_INDEX} has no {CAPABILITY_ID} capability entry"
    return entry


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


def _vitest_case(path: Path, case_name: str) -> str:
    """One `it(...)` case's own source.

    A user-visible half of this capability is evidenced by a vitest case rather
    than a pytest function, and it has to be checkable the same way: the row must
    resolve to a case that exists, and that case must name its catalog ID where a
    Python test would carry a docstring. Slicing the case out is what makes the ID
    check per-row instead of per-file, so a second row cannot ride on the first
    one's ID.
    """

    source = path.read_text()
    start = next(
        (found for quote in ("'", '"') if (found := source.find(f"it({quote}{case_name}{quote}")) != -1),
        -1,
    )
    assert start != -1, f"Catalog points to missing UI case {path}::{case_name}"
    end = source.find("\n  it(", start + 1)
    return source[start:] if end == -1 else source[start:end]


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
    """MH-CATALOG-001: every live row resolves, names its own ID, and is reachable from the project index."""

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
        if path.suffix in {".ts", ".tsx"}:
            # This checker cannot read a vitest modifier, so a UI-evidenced row may
            # not claim an expected failure it has no way to prove.
            assert not status["expected_fail"], (
                f"Scenario {scenario['id']} is evidenced by a UI case, which cannot carry an expected failure"
            )
            evidence = _vitest_case(path, function_name)
        else:
            function = _test_function(path, function_name)
            expected_fail = "xfail" in _decorator_names(function)
            assert expected_fail == status["expected_fail"]
            evidence = ast.get_docstring(function) or ""
        assert scenario["id"] in evidence, (
            f"Scenario {scenario['id']} is not named inside {test_ref}; "
            "state the catalog ID in the test docstring so the executable evidence is greppable by ID"
        )
        if scenario["status"] == "partial":
            missing_layer = scenario.get("missing_layer")
            assert missing_layer and missing_layer != scenario["layer"], (
                f"Scenario {scenario['id']} is partial, so it must name the unproved layer in missing_layer"
            )

    entry = _indexed_capability()
    assert entry["catalog"] == (SCENARIO_ROOT / "catalog.yaml").as_posix()
    assert entry["observations"] == (SCENARIO_ROOT / "observations.yaml").as_posix()
    indexed_tests = {
        item
        for key in ("scenario_tests", "harness", "unit_or_contract_tests")
        for item in entry.get(key, [])
    }
    assert all(Path(item).is_file() for item in indexed_tests)
    assert canonical_tests <= indexed_tests, (
        f"Canonical Model Hub evidence missing from {PROJECT_INDEX}: {sorted(canonical_tests - indexed_tests)}"
    )

    observation_ids = [item["id"] for item in observations["observations"]]
    assert len(observation_ids) == len(set(observation_ids))
    assert all(
        scenario_id in scenario_by_id
        for observation in observations["observations"]
        for scenario_id in observation["affects"]
    )
