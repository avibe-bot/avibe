"""Every case a scenario catalog cites is one pytest collects, and names the row citing it.

A row's citations are how a reader gets from a scenario to the code that runs
it: ``test`` and ``partial_evidence`` name the evidence, and the pointer keys
``related_tests`` and ``supporting_tests`` name neighbouring cases. Only the
Model Hub catalog checked its own evidence keys (MH-CATALOG-001), so renamed
or deleted tests left nine rows across two catalogs reading ``covered`` while
the case they cited no longer existed. Every catalog's legend defines
``covered`` as automated coverage that exists, so a ``covered`` row must cite it.

Python citations resolve against pytest's own collection of the cited files,
as UI citations resolve against vitest's
(``ui/scripts/validate-scenario-catalog.mjs``; here only their file is
checked). A source read approximated collection and missed what pytest
decides: a ``Test*`` class with ``__init__`` or ``__test__ = False``, a
deselecting hook, a test defined in a branch not taken.

- An evidence key (``test``, ``partial_evidence.test``) names exactly one
  collected case (STANDARD.md §4): ``file::Class::method``, ``file::name`` for a
  module-level test or the one method of that name, or either with a
  parametrized id. A parametrized function is one case. A bare file or a test
  class stays collected for whatever else cites it, so it evidences no row in
  particular; a row whose contract several cases prove lists each of them
  under ``test``, and ``partial_evidence.test`` names one. The case must not be
  skipped unconditionally (``pytest.mark.skip`` on it, its class or its module,
  or ``unittest.skip``), and its docstring must name the citing row's ID, so the
  evidence is greppable by ID and an unrelated test cannot quietly stand in for
  a row (AGENTS.md §7).
- A pointer key leads to at least one collected case: a bare file with any, or
  a name contained in a case's node id -- a class, a function, a method -- or a
  scenario ID that a case's docstring names, which is the group of cases
  carrying that row.

A cited Python file must be one pytest's default discovery collects
(``test_*.py`` or ``*_test.py``). Conditional skips (``skipif``) depend on the
environment; whether each case a ``covered`` row cites actually ran is decided
from CI's own results by ``scripts/ci_scenario_evidence.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
import yaml

from scripts import pytest_collection

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = REPO_ROOT / "tests" / "scenarios"
_UI_SUFFIXES = {".ts", ".tsx", ".mts", ".mjs"}
_POINTER_KEYS = ("related_tests", "supporting_tests")
# pytest's default ``python_files``, which this repository does not override.
_DISCOVERED = re.compile(r"(test_.*|.*_test)\.py")


@dataclass(frozen=True)
class _Case:
    """One collected item: its path within the file, its parametrized id, and what it declares."""

    path: tuple[str, ...]
    param: str
    doc: str
    skipped: bool


def _catalogs() -> list[Path]:
    catalogs = sorted(SCENARIO_ROOT.glob("*/catalog.yaml"))
    assert catalogs, f"no scenario catalogs under {SCENARIO_ROOT}"
    return catalogs


def _rows(catalog: Path) -> list[dict]:
    return yaml.safe_load(catalog.read_text(encoding="utf-8")).get("scenarios") or []


def _citations(row: dict) -> tuple[list[tuple[str, object]], list[str]]:
    """Each (key, reference) the row cites a test file or case with, and each malformed key.

    A malformed key is reported rather than skipped: read as absent, ``test: []``
    or a ``partial_evidence`` with no reference would cite nothing and pass.
    """

    cited: list[tuple[str, object]] = []
    malformed: list[str] = []
    evidence = row.get("test")
    if evidence is None and row.get("status") == "covered":
        malformed.append("is covered but cites no test")
    if isinstance(evidence, list):
        # A row whose contract several cases prove names each of them.
        if not evidence:
            malformed.append("test lists no case")
        cited.extend(("test", reference) for reference in evidence)
    elif evidence is not None:
        cited.append(("test", evidence))
    if "partial_evidence" in row:
        partial = row["partial_evidence"]
        if isinstance(partial, dict) and isinstance(partial.get("test"), list):
            malformed.append("partial_evidence lists cases; it names one, and only test may list several")
        elif isinstance(partial, dict) and "test" in partial:
            cited.append(("partial_evidence", partial["test"]))
        else:
            malformed.append("partial_evidence names no test")
    for key in _POINTER_KEYS:
        pointers = row.get(key)
        if isinstance(pointers, list):
            cited.extend((key, reference) for reference in pointers)
        elif pointers is not None:
            malformed.append(f"{key} is not a list")
    return cited, malformed


def _collect_cases(files: list[str], cwd: Path) -> dict[str, list[_Case]]:
    """Every case pytest collects from ``files``, relative to ``cwd``, by file."""

    cases: dict[str, list[_Case]] = {file: [] for file in files}
    for entry in pytest_collection.collect(files, cwd):
        file, _, rest = entry["nodeid"].partition("::")
        base, bracket, param = rest.partition("[")
        cases.setdefault(file, []).append(
            _Case(tuple(base.split("::")), bracket + param, entry["doc"], entry["skipped"])
        )
    return cases


@cache
def _collected() -> dict[str, list[_Case]]:
    """Every case pytest collects from the Python files any catalog cites, by file."""

    files = {
        reference.partition("::")[0]
        for catalog in _catalogs()
        for row in _rows(catalog)
        for _key, reference in _citations(row)[0]
        if isinstance(reference, str)
    }
    return _collect_cases(
        sorted(
            file
            for file in files
            if file.endswith(".py") and _DISCOVERED.fullmatch(Path(file).name) and (REPO_ROOT / file).is_file()
        ),
        REPO_ROOT,
    )


def _names_id(doc: str, scenario_id: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(scenario_id)}(?![\w-])", doc) is not None


def _evidence_problem(cases: list[_Case], name: str, row_id: str) -> str | None:
    """Why ``name`` is not the one runnable case that evidences row ``row_id``; None when it is."""

    base, bracket, param = name.partition("[")
    parts = tuple(base.split("::"))
    matches = [
        case
        for case in cases
        if (case.path == parts or (len(parts) == 1 and case.path[-1] == parts[0]))
        and (not bracket or case.param == bracket + param)
    ]
    named = {case.path for case in matches}
    if not named:
        if len(parts) == 1 and any(len(case.path) > 1 and parts[0] in case.path[:-1] for case in cases):
            return "names a test class, not one case; cite each case that proves the row"
        return "names no case pytest collects in the file"
    if len(named) != 1:
        return f"names {len(named)} cases, expected exactly one; cite it as Class::method"
    if all(case.skipped for case in matches):
        return "is skipped unconditionally, so it never runs"
    if not _names_id(matches[0].doc, row_id):
        return f"does not name {row_id} in its docstring; state the row's ID there so the evidence is greppable by it"
    return None


def _pointer_leads_somewhere(cases: list[_Case], name: str, scenario_ids: frozenset[str]) -> bool:
    if not name:
        return bool(cases)
    return any(name in "::".join(case.path) + case.param for case in cases) or (
        name in scenario_ids and any(_names_id(case.doc, name) for case in cases)
    )


@cache
def _scenario_ids() -> frozenset[str]:
    return frozenset(row["id"] for catalog in _catalogs() for row in _rows(catalog) if isinstance(row.get("id"), str))


@pytest.mark.parametrize("catalog", _catalogs(), ids=lambda path: path.parent.name)
def test_every_catalog_citation_resolves_to_a_case(catalog: Path) -> None:
    dead: list[str] = []
    for row in _rows(catalog):
        cited, malformed = _citations(row)
        dead.extend(f"{row['id']} {problem}" for problem in malformed)
        for key, reference in cited:
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
            if path.suffix in _UI_SUFFIXES:
                continue
            if path.suffix == ".py" and not _DISCOVERED.fullmatch(path.name):
                dead.append(f"{where} (pytest's default discovery never collects {path.name})")
                continue
            cases = _collected().get(path_text, [])
            if pointer:
                # A pointer claims no evidence, so a class or a group of cases is a fine destination.
                if not _pointer_leads_somewhere(cases, name if separator else "", _scenario_ids()):
                    dead.append(f"{where} (leads to no case pytest collects)")
                continue
            problem = _evidence_problem(cases, name, row["id"])
            if problem:
                dead.append(f"{where} ({problem})")
    assert not dead, f"{catalog.relative_to(REPO_ROOT)} has citations that resolve to no case of theirs:\n" + "\n".join(dead)


# Cases a source read takes for collected, and pytest does not run (#2565).
_SHAPES = '''
import pytest


class TestSuppressed:
    __test__ = False

    def test_hidden(self):
        """ROW-1"""


class TestWithInit:
    def __init__(self):
        pass

    def test_also_hidden(self):
        """ROW-1"""


@pytest.mark.skip(reason="never")
class TestSkipped:
    def test_never_runs(self):
        """ROW-1"""


def test_deselected():
    """ROW-1"""


@pytest.mark.parametrize("value", [1, 2])
def test_carries_the_row(value):
    """ROW-1: a case named for the row that cites it."""


def test_carries_another_row():
    """ROW-2"""
'''

_DESELECTING_CONFTEST = '''
def pytest_collection_modifyitems(config, items):
    deselected = [item for item in items if item.name == "test_deselected"]
    config.hook.pytest_deselected(items=deselected)
    items[:] = [item for item in items if item not in deselected]
'''


def test_a_citation_resolves_only_to_a_case_pytest_collects_and_runs(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(_DESELECTING_CONFTEST, encoding="utf-8")
    (tmp_path / "test_shapes.py").write_text(_SHAPES, encoding="utf-8")

    cases = _collect_cases(["test_shapes.py"], tmp_path)["test_shapes.py"]

    def problem(name: str, row_id: str = "ROW-1") -> str | None:
        return _evidence_problem(cases, name, row_id)

    for hidden in ("TestSuppressed::test_hidden", "test_also_hidden", "test_deselected"):
        assert problem(hidden) == "names no case pytest collects in the file", hidden
    assert problem("TestSkipped::test_never_runs") == "is skipped unconditionally, so it never runs"
    assert problem("test_carries_the_row") is None
    assert problem("test_carries_the_row[2]") is None
    assert problem("test_carries_another_row") == (
        "does not name ROW-1 in its docstring; state the row's ID there so the evidence is greppable by it"
    )
    # A pointer may name the group of cases that carry a row.
    ids = frozenset({"ROW-1", "ROW-2", "ROW-3"})
    assert _pointer_leads_somewhere(cases, "ROW-1", ids)
    assert not _pointer_leads_somewhere(cases, "ROW-3", ids)
