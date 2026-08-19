"""Mechanical integrity checks for the Model Hub scenario catalog."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml


SCENARIO_ROOT = Path("tests/scenarios/model_hub")
PROJECT_INDEX = Path("tests/scenarios/INDEX.yaml")
CAPABILITY_ID = "model_hub"

#: A vitest case declaration: the bare `it(` call form followed by a name literal.
#: The lookbehind rejects `xit(` and `suite.it(`, and requiring `(` immediately
#: after the identifier rejects every modifier form (`it.skip`, `it.each`, …).
_IT_CALL = re.compile(r"(?<![A-Za-z0-9_$.])it\s*\(\s*(?=['\"`])")


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


def _advance(source: str, index: int) -> int:
    """Index just past the comment, literal, or regex at `index`; the next character otherwise.

    This is the whole lexer: everything the scan must not read as code is a comment,
    a literal, or a regex, and skipping one is always "jump to where it ends".
    """

    pair = source[index : index + 2]
    if pair == "//":
        end = source.find("\n", index)
        return len(source) if end == -1 else end
    if pair == "/*":
        end = source.find("*/", index)
        return len(source) if end == -1 else end + 2
    if source[index] in "'\"`":
        return _skip_literal(source, index)
    if source[index] == "/" and _opens_regex(source, index):
        return _skip_regex(source, index)
    return index + 1


def _skip_literal(source: str, index: int) -> int:
    """Index just past the string or template literal opening at `index`."""

    quote = source[index]
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
        elif char == quote:
            return index + 1
        elif quote == "`" and char == "$" and source[index + 1 : index + 2] == "{":
            # A substitution is code again, so comments and nested literals apply.
            index = _skip_substitution(source, index + 1)
        else:
            index += 1
    return index


def _skip_substitution(source: str, index: int) -> int:
    """Index just past the balanced `{`…`}` of a template substitution opening at `index`."""

    depth = 0
    while index < len(source):
        char = source[index]
        if char == "{":
            depth += 1
            index += 1
        elif char == "}":
            depth -= 1
            index += 1
            if not depth:
                return index
        else:
            index = _advance(source, index)
    return index


def _opens_regex(source: str, index: int) -> bool:
    """Whether the `/` at `index` opens a regex rather than dividing.

    Only the previous token can tell the two apart, and the distinction matters
    because a regex holding an apostrophe — `/Couldn't refresh/i`, ordinary in this
    suite's assertions — reads as a string opening otherwise. A value can be divided,
    so a `/` after a name, a number, or a closing `)` / `]` is division; every other
    position is one where a value has to start.
    """

    before = source[:index].rstrip()
    return not before or not (before[-1].isalnum() or before[-1] in "_$)]")


def _skip_regex(source: str, index: int) -> int:
    """Index just past the regex literal opening at `index`.

    A character class is tracked because `/` inside one needs no escape (`/[/]/`),
    and an unterminated line ends the scan: no regex spans a newline, so a division
    misread as a regex costs one line rather than the rest of the file.
    """

    index += 1
    within_class = False
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
        elif char == "\n":
            return index
        elif char == "[":
            within_class = True
            index += 1
        elif char == "]":
            within_class = False
            index += 1
        elif char == "/" and not within_class:
            return index + 1
        else:
            index += 1
    return index


def _vitest_case_names(source: str) -> list[str]:
    """The name of every executable `it('name', …)` declaration, in source order."""

    names: list[str] = []
    index = 0
    while index < len(source):
        match = _IT_CALL.match(source, index)
        if match is None:
            index = _advance(source, index)
            continue
        end = _skip_literal(source, match.end())
        names.append(source[match.end() + 1 : end - 1])
        index = end
    return names


def _vitest_case_name(path: Path, scenario_id: str) -> str:
    """The full name of the one executable vitest case that carries `scenario_id`.

    A user-visible half of this capability is evidenced by a vitest case rather than
    a pytest function, and it has to be checkable the same way: the row must resolve
    to a case that really runs, and that case must state its catalog ID where a
    Python test would carry a docstring. A vitest case's name *is* its docstring, so
    that is where the ID goes — which also makes the evidence runnable by ID
    (`vitest -t MH-USAGE-018`) rather than merely greppable.

    Reading names alone is what keeps this fail-closed. Resolution has to be lexical,
    because every shape that would fool a text search executes nothing: a
    commented-out case is not a declaration and neither is a name quoted inside a
    string. One shape a scan this size cannot decide is a regex literal against
    division, since that needs the previous token's type rather than its last
    character. Because a name is all this reads, the worst a wrong guess can do is
    lose a declaration — the row stops resolving, loudly. It cannot hand a row a
    neighbour's ID, which is exactly what an earlier version that sliced each case's
    body got wrong: it bounded the slice by re-running this same scan, so a desync
    agreed with itself and the row passed on a sibling's ID.

    Only the bare `it(` form resolves, so a modifier this checker cannot verify
    (`it.skip`, `it.only`, `it.fails`) never counts as evidence, and a parameterized
    `it.each` — whose names are built at run time — cannot be cited as a row's proof.
    """

    prefix = f"{scenario_id}:"
    names = [name for name in _vitest_case_names(path.read_text()) if name.startswith(prefix)]
    assert len(names) == 1, (
        f"Catalog points to {len(names)} executable UI cases named `{prefix} …` in {path}, expected exactly one; "
        "a commented-out, skipped, parameterized, or merely quoted case is not executable evidence"
    )
    return names[0]


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
            # Only the bare `it(` form resolves, so a UI-evidenced row has no way to
            # declare an expected failure and may not claim one.
            assert not status["expected_fail"], (
                f"Scenario {scenario['id']} is evidenced by a UI case, which cannot carry an expected failure"
            )
            # A UI row cites its ID, and the case it resolves to is the evidence that
            # the ID names something vitest runs.
            evidence = _vitest_case_name(path, function_name)
        else:
            function = _test_function(path, function_name)
            expected_fail = "xfail" in _decorator_names(function)
            assert expected_fail == status["expected_fail"]
            evidence = ast.get_docstring(function) or ""
        assert scenario["id"] in evidence, (
            f"Scenario {scenario['id']} is not named inside {test_ref}; "
            "state the catalog ID in the test docstring — for a UI case, in its name — "
            "so the executable evidence is greppable by ID"
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


#: Executable, and so citable as evidence.
_RUNS = "MH-FIXTURE-RUNS"
#: Present in the file, executed by nothing, and so citable by nothing.
_DEAD = "MH-FIXTURE-DEAD"

#: One declaration of every shape a `.tsx` file can carry: each is named for whether
#: vitest runs it, and the assertions read that name rather than a list of cases. A
#: shape added here is covered by the same two properties without editing them.
#:
#: The apostrophe inside a regex literal is what a scan gets wrong by default —
#: read as a string opening, it swallows every declaration that follows, which is
#: why a live case sits after it.
_RESOLVER_FIXTURE = f"""
describe('resolver fixture', () => {{
  // it('{_DEAD}-COMMENTED: commented out', () => {{}});
  /* it('{_DEAD}-BLOCK: commented out in a block', () => {{}}); */
  it.skip('{_DEAD}-SKIPPED: declared with a modifier', () => {{}});
  it.each([1, 2])('{_DEAD}-EACH: named at run time %i', () => {{}});
  it('{_RUNS}-QUOTES: quotes declarations and matches on apostrophes', () => {{
    expect(label).toBe("it('{_DEAD}-INSTRING: quoted in a string', () => {{}})");
    expect(hint).toBe(`it('{_DEAD}-INTEMPLATE: quoted in a template', ${{suffix}})`);
    expect(text).toMatch(/Couldn't refresh, please retry/i);
    expect(share).toBe(total / count);
  }});
  it('{_RUNS}-AFTER: runs after every shape above', () => {{
    expect(true).toBe(true);
  }});
}});
"""


def test_mh_catalog_002_ui_evidence_resolves_only_to_an_executable_declaration(tmp_path: Path) -> None:
    """MH-CATALOG-002: a UI-evidenced row resolves to the one vitest case that runs under its ID.

    The failure this closes is the one a text search cannot tell apart from evidence:
    a catalog ID that appears in a `.tsx` file which vitest executes no matching case
    from, leaving a row `covered` by text alone.
    """

    path = tmp_path / "fixture.test.tsx"
    path.write_text(_RESOLVER_FIXTURE)
    resolved = _vitest_case_names(_RESOLVER_FIXTURE)

    # Nothing dead resolves, and nothing live is missed — a scan that desynced on the
    # regex above would come up short here rather than resolve past its declaration.
    assert all(name.startswith(_RUNS) for name in resolved), resolved
    assert len(resolved) == _RESOLVER_FIXTURE.count(f"'{_RUNS}")

    # A row cites an ID, which resolves to exactly the case that carries it.
    for name in resolved:
        assert _vitest_case_name(path, name.split(":")[0]) == name
    with pytest.raises(AssertionError):
        _vitest_case_name(path, _DEAD)
