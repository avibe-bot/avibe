"""Mechanical integrity checks for the Model Hub scenario catalog."""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


SCENARIO_ROOT = Path("tests/scenarios/model_hub")
PROJECT_INDEX = Path("tests/scenarios/INDEX.yaml")
CAPABILITY_ID = "model_hub"

#: Suffixes whose evidence is a vitest case rather than a pytest function. Kept in
#: step with `UI_SUFFIXES` in `ui/scripts/scenarioCatalog.mjs`, which resolves those
#: rows against vitest's own collection.
_UI_SUFFIXES = {".ts", ".tsx", ".mts", ".mjs"}


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


def _decorator_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in function.decorator_list:
        node = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def _resolve_test_evidence(
    *,
    scenario_id: str,
    test_ref: object,
    canonical_tests: set[str],
    expected_fail: bool,
) -> None:
    assert isinstance(test_ref, str), (
        f"Scenario {scenario_id} has no executable test reference"
    )
    path_text, separator, function_name = test_ref.partition("::")
    assert separator and path_text and function_name, (
        f"Scenario {scenario_id} has malformed test reference {test_ref!r}"
    )
    path = Path(path_text)
    assert path_text in canonical_tests
    assert path.is_file()
    if path.suffix in _UI_SUFFIXES:
        # Expected failures are declared with pytest markers, which UI cases
        # cannot carry. Vitest validates actual UI collection separately.
        assert not expected_fail, (
            f"Scenario {scenario_id} is evidenced by a UI case, which "
            "cannot carry an expected failure"
        )
        evidence = path.read_text()
    else:
        function = _test_function(path, function_name)
        actual_expected_fail = "xfail" in _decorator_names(function)
        assert actual_expected_fail == expected_fail
        evidence = ast.get_docstring(function) or ""
    assert scenario_id in evidence, (
        f"Scenario {scenario_id} is not named inside {test_ref}; "
        "state the catalog ID in the test docstring — for a UI case, in "
        "its name — so the executable evidence is greppable by ID"
    )


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
        else:
            _resolve_test_evidence(
                scenario_id=scenario["id"],
                test_ref=test_ref,
                canonical_tests=canonical_tests,
                expected_fail=status["expected_fail"],
            )

        partial_evidence = scenario.get("partial_evidence")
        if partial_evidence is not None:
            assert isinstance(partial_evidence, dict)
            assert partial_evidence.get("covers")
            _resolve_test_evidence(
                scenario_id=scenario["id"],
                test_ref=partial_evidence.get("test"),
                canonical_tests=canonical_tests,
                expected_fail=False,
            )
        if scenario["status"] == "partial":
            missing_layer = scenario.get("missing_layer")
            assert missing_layer and missing_layer != scenario["layer"], (
                f"Scenario {scenario['id']} is partial, so it must name the unproved layer in missing_layer"
            )
        if scenario["status"] == "skip":
            for field in ("reason", "owning_layer"):
                value = scenario.get(field)
                assert isinstance(value, str) and value.strip(), (
                    f"Scenario {scenario['id']} is skipped, so {field} must "
                    "name the blocking boundary"
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
