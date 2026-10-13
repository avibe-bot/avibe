"""Every case a scenario catalog's pointer keys name still exists, in every catalog.

``related_tests`` and ``supporting_tests`` point a reader from a scenario to
neighbouring cases. They claim no coverage, so nothing read them, and a renamed
or deleted test left the pointer leading nowhere: six had, in two catalogs.
Evidence keys are another check's subject: MH-CATALOG-001 resolves Model Hub's,
and the UI gate resolves every catalog's UI evidence.

A pointer is a list. Each entry names a file, which must exist, and may name a
case in it: ``file::Class::method``, or ``file::name`` naming a module-level
test function, a test class, or a test method of a class in the file. pytest's
default naming rules decide what is a test, so a helper or a plain class is
not. This reads source and matches exact names; it does not emulate
collection-time exclusions such as ``__test__ = False``, which #2565 tracks.

UI pointers are resolved against vitest's own collection by
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
def _test_names(path: Path) -> frozenset[str]:
    """Every name in ``path`` that is a test pytest's default rules collect, or a test class.

    A module-level ``test*`` function; a ``Test*`` class or a ``unittest.TestCase``
    subclass, directly or through another class in the file; and each ``test*``
    method of such a class, by itself and as ``Class::method``.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def is_test_case(name: str, seen: frozenset[str] = frozenset()) -> bool:
        node = classes.get(name)
        if node is None or name in seen:
            return False
        bases = [base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "") for base in node.bases]
        return any(base.endswith("TestCase") or is_test_case(base, seen | {name}) for base in bases)

    def test_functions(body: list[ast.stmt]) -> set[str]:
        return {
            node.name
            for node in body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")
        }

    names = test_functions(tree.body)
    for name, node in classes.items():
        if name.startswith("Test") or is_test_case(name):
            names.add(name)
            for method in test_functions(node.body):
                names.update({method, f"{name}::{method}"})
    return frozenset(names)


def _catalogs() -> list[Path]:
    catalogs = sorted(SCENARIO_ROOT.glob("*/catalog.yaml"))
    assert catalogs, f"no scenario catalogs under {SCENARIO_ROOT}"
    return catalogs


@pytest.mark.parametrize("catalog", _catalogs(), ids=lambda path: path.parent.name)
def test_every_pointer_names_a_file_or_case_that_exists(catalog: Path) -> None:
    rows = yaml.safe_load(catalog.read_text(encoding="utf-8")).get("scenarios") or []
    dead: list[str] = []
    for row in rows:
        for key in _POINTER_KEYS:
            pointers = row.get(key)
            if pointers is None:
                continue
            if not isinstance(pointers, list):
                dead.append(f"{row['id']} {key} is not a list")
                continue
            for reference in pointers:
                where = f"{row['id']} {key}: {reference!r}"
                if not isinstance(reference, str):
                    dead.append(f"{where} (not a reference)")
                    continue
                path_text, separator, name = reference.partition("::")
                path = REPO_ROOT / path_text
                if not path.is_file():
                    dead.append(f"{where} (no such file)")
                elif separator and not name.strip():
                    dead.append(f"{where} (names an empty case)")
                elif separator and path.suffix not in _UI_SUFFIXES:
                    if name.partition("[")[0] not in _test_names(path):
                        dead.append(f"{where} (no such test in the file)")
    assert not dead, f"{catalog.relative_to(REPO_ROOT)} has pointers that lead nowhere:\n" + "\n".join(dead)
