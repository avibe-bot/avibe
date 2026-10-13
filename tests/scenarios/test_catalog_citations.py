"""Every pytest case a scenario catalog cites still exists, in every catalog and every key.

A row's citations are how a reader gets from a scenario to the code that runs
it: ``test`` and ``partial_evidence`` name the evidence, and the pointer keys
``related_tests`` and ``supporting_tests`` name neighbouring cases. Only the
Model Hub catalog checked its own evidence keys (MH-CATALOG-001), so renamed
or deleted tests left dead citations everywhere else: related pointers that
named nothing, and nine rows across two catalogs reading ``covered`` while the
case they cited no longer existed.

A pytest citation resolves when its file holds a case pytest collects:
``file::Class::method``, or ``file::name`` naming a module-level test, a test
class, or a test method of a class in that file, which is how several catalogs
cite unittest cases. A helper or a plain class is no case. A parametrized id
resolves through its function. An evidence key names exactly one case per
entry (STANDARD.md §4): a bare file or a test class stays collected for
whatever else cites it, so it evidences no row in particular, and a row whose
contract several cases prove lists each of them under ``test``. Pointer keys
may name a bare file, a class or a group of cases, which must exist.

UI (vitest) citations are resolved against vitest's own collection by
``ui/scripts/validate-scenario-catalog.mjs``; here only their file is checked.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "tests" / "scenarios"
_UI_SUFFIXES = {".ts", ".tsx", ".mts", ".mjs"}
_POINTER_KEYS = ("related_tests", "supporting_tests")


@cache
def _collectable_cases(path: Path) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """The test functions pytest collects from ``path``, and each test class's test methods.

    pytest's default rules, which this repository does not change: a module-level
    ``test*`` function, and a ``test*`` method of a ``Test*`` class or of a
    ``unittest.TestCase`` subclass, directly or through another class in the file.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def base_names(node: ast.ClassDef) -> list[str]:
        return [base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "") for base in node.bases]

    def is_test_case(name: str, seen: frozenset[str] = frozenset()) -> bool:
        node = classes.get(name)
        if node is None or name in seen:
            return False
        return any(
            base.endswith("TestCase") or is_test_case(base, seen | {name}) for base in base_names(node)
        )

    def test_names(body: list[ast.stmt]) -> frozenset[str]:
        return frozenset(
            node.name
            for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
        )

    test_classes = {
        name: test_names(node.body)
        for name, node in classes.items()
        if name.startswith("Test") or is_test_case(name)
    }
    return test_names(tree.body), test_classes


def _named_cases(path: Path, name: str) -> tuple[int, bool]:
    """How many collected cases ``name`` names in ``path``, and whether it names a test class."""

    functions, classes = _collectable_cases(path)
    parts = name.partition("[")[0].split("::")
    if len(parts) == 2:
        return int(parts[1] in classes.get(parts[0], frozenset())), False
    if len(parts) != 1:
        return 0, False
    (case,) = parts
    return int(case in functions) + sum(case in methods for methods in classes.values()), case in classes


def _citations(row: dict) -> list[tuple[str, object]]:
    """Each (key, reference) the row cites a test file or case with."""

    cited: list[tuple[str, object]] = []
    evidence = row.get("test")
    # A row whose contract several cases prove names each of them.
    for reference in evidence if isinstance(evidence, list) else [evidence] if evidence is not None else []:
        cited.append(("test", reference))
    partial = row.get("partial_evidence")
    if isinstance(partial, dict) and isinstance(partial.get("test"), str):
        cited.append(("partial_evidence", partial["test"]))
    for key in _POINTER_KEYS:
        cited.extend((key, reference) for reference in row.get(key) or [])
    return cited


def _catalogs() -> list[Path]:
    catalogs = sorted(SCENARIO_ROOT.glob("*/catalog.yaml"))
    assert catalogs, f"no scenario catalogs under {SCENARIO_ROOT}"
    return catalogs


@pytest.mark.parametrize("catalog", _catalogs(), ids=lambda path: path.parent.name)
def test_every_catalog_citation_resolves_to_a_case(catalog: Path) -> None:
    rows = yaml.safe_load(catalog.read_text(encoding="utf-8")).get("scenarios") or []
    dead: list[str] = []
    for row in rows:
        for key, reference in _citations(row):
            where = f"{row['id']} {key}: {reference}"
            if not isinstance(reference, str):
                dead.append(f"{row['id']} {key}: {reference!r} is not a reference")
                continue
            path_text, separator, name = reference.partition("::")
            path = REPO_ROOT / path_text
            pointer = key in _POINTER_KEYS
            if not pointer and not name.strip():
                dead.append(f"{where} (evidence must name a case, not only its file)")
                continue
            if not path.is_file():
                dead.append(f"{where} (no such file)")
                continue
            if not separator or path.suffix in _UI_SUFFIXES:
                continue
            count, names_class = _named_cases(path, name)
            if pointer:
                # A pointer claims no evidence, so a class or a group of cases is a fine destination.
                if not count and not names_class:
                    dead.append(f"{where} (no such case in the file)")
            elif names_class:
                dead.append(f"{where} (names a test class, not one case; cite each case that proves the row)")
            elif count != 1:
                dead.append(f"{where} (names {count} cases, expected exactly one; cite it as Class::method)")
    assert not dead, f"{catalog.relative_to(REPO_ROOT)} has citations that resolve to no case:\n" + "\n".join(dead)
