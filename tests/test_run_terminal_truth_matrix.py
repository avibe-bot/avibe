"""HFR-184..HFR-187, HFR-189..HFR-190, HFR-192..HFR-194: the PR7R matrix is closed.

Same contract as HFR-105 for ``TEARDOWN_SETTLEMENT_MATRIX``: growing a
dimension fails here until every new cell names a consuming test or a precise
ownership reason. PR7R adds two obligations HFR-105 does not have -- every
named node id must resolve to a real test function, and the ``unproven`` count
must equal the checked-in budget so a gap cannot widen or be papered over
silently.
"""

import ast
import re
from pathlib import Path

import pytest

from tests.run_terminal_truth_evidence import (
    BACKENDS,
    EXACT_TURN_PROGRESS_SIGNALS,
    LANES,
    OUTCOMES,
    PR7R_FINDINGS,
    PR7R_QUESTIONS,
    RUN_TERMINAL_TRUTH_MATRIX,
    TRIGGERS,
    UNPROVEN_BUDGET,
)

_PROOF_KINDS = {"covered", "shared", "N/A", "defect", "unproven"}


def _expand() -> list[tuple[str, str, str, str, tuple[str, str]]]:
    """The full backend x lane x trigger x outcome product.

    A cell written once for the lane applies to every backend; the expansion is
    what makes that a claim about all three rather than a silent omission.
    """
    cells = []
    for backend in BACKENDS:
        for lane in LANES:
            for trigger in TRIGGERS:
                rows = RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)]
                for outcome in OUTCOMES:
                    cell = rows[outcome]
                    proof = cell.get("per_backend", {}).get(backend, cell["shared"])
                    cells.append((backend, lane, trigger, outcome, proof))
    return cells


_CELLS = _expand()


def _assert_symbol_exists(qualified: str) -> None:
    """Resolve a COMPLETE ``path::Class::symbol`` id, one nesting level at a time.

    Matching only the trailing function name and walking the whole module --
    which is what HFR-105 does, and what this guard did first -- accepts an id
    whose class component is misspelled, renamed, or absent, as long as some
    same-named function exists anywhere in the file. For a matrix whose entire
    value is that its citations are real, that is the wrong failure mode: the
    citation would stay green while pointing at a test that no longer runs.

    Used for BOTH halves of the unit. Test citations were validated this way
    from the start; finding OWNERS were not -- the guard split on ``::`` and
    checked only that the module file existed, so
    ``running_agents.py::end_running_agent`` stayed green through a rename and
    the unit went on advertising a contract owner no amendment could locate.
    Applying full validation to citations and none to owners was the same
    asymmetry twice over, so there is now one resolver.
    """
    module_path, *parts = qualified.split("::")
    assert parts, qualified
    source = Path(module_path)
    assert source.exists(), f"{qualified}: no module at {module_path}"

    scope: list[ast.stmt] = ast.parse(source.read_text(encoding="utf-8")).body
    for depth, name in enumerate(parts):
        is_leaf = depth == len(parts) - 1
        # A leaf may be a function; a non-leaf must be a class. A leaf that is
        # itself a class is allowed -- an owner may legitimately be a type.
        wanted: tuple[type[ast.AST], ...] = (
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            if is_leaf
            else (ast.ClassDef,)
        )
        match = next(
            (
                node
                for node in scope
                if isinstance(node, wanted) and node.name == name
            ),
            None,
        )
        assert match is not None, (
            f"{qualified}: no {'symbol' if is_leaf else 'class'} "
            f"named {name!r} at this level"
        )
        scope = match.body


def _assert_node_exists(node_id: str) -> None:
    """A pytest node id: the same resolution, rooted under ``tests/``."""
    assert node_id.split("::")[0].startswith("tests/"), node_id
    _assert_symbol_exists(node_id)


def _detail_node(detail: str) -> str:
    """``shared``/``defect`` details carry ``<claim> -- <node id>``."""
    claim, marker, node_id = detail.rpartition(" -- ")
    assert marker, detail
    assert claim.strip(), detail
    return node_id.strip()


@pytest.mark.parametrize(
    ("backend", "lane", "trigger", "outcome", "proof"),
    _CELLS,
    ids=[f"{b}-{la}-{t}-{o}" for b, la, t, o, _p in _CELLS],
)
def test_every_run_terminal_truth_cell_is_proven_or_named_as_a_gap(
    backend: str,
    lane: str,
    trigger: str,
    outcome: str,
    proof: tuple[str, str],
) -> None:
    """HFR-184: every cell of the PR7R matrix carries an explicit proof."""
    assert set(RUN_TERMINAL_TRUTH_MATRIX) == {
        (lane_key, trigger_key) for lane_key in LANES for trigger_key in TRIGGERS
    }
    assert set(RUN_TERMINAL_TRUTH_MATRIX[(lane, trigger)]) == set(OUTCOMES)

    kind, detail = proof
    assert kind in _PROOF_KINDS, proof
    assert detail.strip(), proof

    if kind == "covered":
        _assert_node_exists(detail)
    elif kind in {"shared", "defect"}:
        _assert_node_exists(_detail_node(detail))
    elif kind == "unproven":
        # A gap is only useful if it says what would close it.
        assert "probe" in detail.lower(), proof
    else:  # N/A
        assert len(detail.split()) >= 4, proof


def test_the_unproven_count_matches_the_checked_in_budget() -> None:
    """HFR-185: the size of the gap is a number, not an impression.

    Lowering ``UNPROVEN_BUDGET`` requires writing the probe named in the cell;
    raising it requires saying so in the same commit. Either way the reviewer
    sees the delta instead of inferring it from prose.
    """
    unproven = [cell for cell in _CELLS if cell[4][0] == "unproven"]
    assert len(unproven) == UNPROVEN_BUDGET, sorted(
        f"{b}-{la}-{t}-{o}" for b, la, t, o, _p in unproven
    )
    assert len(_CELLS) == len(BACKENDS) * len(LANES) * len(TRIGGERS) * len(OUTCOMES)
    assert UNPROVEN_BUDGET <= len(_CELLS)

    # Anti-degeneracy, which is what the old ``UNPROVEN_BUDGET < len(_CELLS)``
    # was reaching for and got wrong. That assertion said "at least one cell
    # must be proven", which is a conclusion about master, not a property of
    # the unit -- and round 3 found it false. The real failure it should catch
    # is someone flipping the matrix to one boilerplate ``unproven`` and
    # calling it honest. So: distinct probes, at least one per (lane, outcome),
    # because a gap that cannot say what would close it is not a gap, it is a
    # shrug.
    distinct = {detail for _b, _la, _t, _o, (kind, detail) in _CELLS if kind == "unproven"}
    assert len(distinct) >= len(LANES) * len(OUTCOMES), len(distinct)


def test_every_question_and_finding_names_a_real_consuming_test() -> None:
    """HFR-186: the five plan questions and both findings are wired to tests."""
    assert set(PR7R_QUESTIONS) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
    for key, entry in PR7R_QUESTIONS.items():
        assert entry["verdict"] in {"answered", "open", "blocked"}, key
        assert entry["question"].strip() and entry["answer"].strip(), key
        assert entry["evidence"], key
        for node_id in entry["evidence"]:
            _assert_node_exists(node_id)

    # Pinned explicitly, exactly as the five questions are one block up. The
    # tie below compares two sets that SHRINK TOGETHER: delete a finding from
    # both ``PR7R_FINDINGS`` and every cell detail and the equality still holds
    # -- an empty matrix passes it. That is the same shape of mistake round 3
    # found in ``UNPROVEN_BUDGET < len(_CELLS)``: an assertion that reads like a
    # floor and enforces nothing. The plan and the HFR-180/HFR-181 catalog
    # entries keep advertising these two, so the guard has to name them.
    assert set(PR7R_FINDINGS) == {"PR7R-F1", "PR7R-F2"}

    for finding_id, finding in PR7R_FINDINGS.items():
        assert finding_id.startswith("PR7R-F"), finding_id
        assert finding["title"].strip() and finding["detail"].strip(), finding_id
        # The owner must resolve to a real SYMBOL, not merely to a real file,
        # so the contract amendment knows exactly who has to change.
        _assert_symbol_exists(finding["owner"])
        _assert_node_exists(finding["reproducer"])

    # Every finding is reachable from the matrix, and every finding id named in
    # the matrix exists. A defect recorded in only one of the two places is how
    # an evidence unit quietly loses a defect.
    #
    # Scanned over EVERY cell rather than only ``defect`` cells, because round 3
    # demoted both defect cells to ``unproven``: the reproducers characterize
    # End's behavior and explicitly disclaim the Run half, which is what those
    # cells are about. The finding is still real and still has to stay tied to
    # the matrix -- the tie just cannot be the proof KIND.
    referenced = {
        finding_id
        for _b, _la, _t, _o, (_kind, detail) in _CELLS
        for finding_id in PR7R_FINDINGS
        if finding_id in detail
    }
    assert referenced == set(PR7R_FINDINGS), referenced
    for _b, _la, _t, _o, (kind, detail) in _CELLS:
        if kind == "defect":
            assert detail.split(" -- ")[0].strip() in PR7R_FINDINGS, detail


def test_a_citation_with_the_wrong_class_component_is_rejected() -> None:
    """HFR-187: the node resolver reads the whole id, not just its last word.

    The regression this pins: a class-qualified citation used to pass on the
    strength of the function name alone, so renaming the class -- or citing a
    class that never existed -- left the matrix pointing at a test pytest would
    not collect, with a green guard. Both real spellings are checked too, so
    the resolver cannot be "fixed" by rejecting everything.
    """
    real = (
        "tests/test_agent_stop_settlement.py::AgentStopSettlementTests::"
        "test_no_backend_stop_uses_the_terminal_turn_default"
    )
    _assert_node_exists(real)  # class-qualified, correct
    _assert_node_exists(  # module-level, correct
        "tests/test_run_terminal_truth_matrix.py::"
        "test_the_unproven_count_matches_the_checked_in_budget"
    )

    with pytest.raises(AssertionError, match="no class named"):
        _assert_node_exists(real.replace("AgentStopSettlementTests", "RenamedTests"))
    with pytest.raises(AssertionError, match="no symbol named"):
        # The class is right; the function moved out from under it.
        _assert_node_exists(
            "tests/test_agent_stop_settlement.py::AgentStopSettlementTests::"
            "test_no_such_test_lives_in_this_class"
        )
    with pytest.raises(AssertionError, match="no class named"):
        # A real module-level function cited as if it were nested.
        _assert_node_exists(
            "tests/test_run_terminal_truth_matrix.py::NotAClass::"
            "test_the_unproven_count_matches_the_checked_in_budget"
        )


def test_a_finding_owner_must_resolve_to_a_real_symbol() -> None:
    """HFR-189: the owner suffix is validated, not decoration.

    The regression this pins: the guard used to keep only the module path and
    assert the FILE existed, so every owner suffix -- the part that says which
    function a contract amendment has to change -- was unchecked. Renaming
    ``end_running_agent``, or misspelling it here in the first place, left the
    unit pointing confidently at nothing while the citation half of the same
    file got full nested-symbol validation.
    """
    owners = {finding["owner"] for finding in PR7R_FINDINGS.values()}
    assert owners, PR7R_FINDINGS
    for owner in owners:
        assert "::" in owner, owner
        _assert_symbol_exists(owner)  # the real spellings

    module_path = next(iter(owners)).split("::")[0]
    with pytest.raises(AssertionError, match="no symbol named"):
        _assert_symbol_exists(f"{module_path}::no_such_owner_function")
    with pytest.raises(AssertionError, match="no module at"):
        _assert_symbol_exists("core/services/not_a_module.py::end_running_agent")


def test_the_q2_signal_table_is_spelled_for_every_backend_and_lane() -> None:
    """HFR-190: Q2's per-backend/per-lane obligation has no missing cells.

    The verdict is tied to the table rather than pinned to a literal. Q2 opened
    once a backend was shown to carry an exact-Turn attribution, and it may only
    close when EVERY cell does -- the plan's rule is all-or-nothing, so a partial
    table and an ``answered`` verdict cannot both be true.
    """
    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    kinds = {kind for kind, _reason in EXACT_TURN_PROGRESS_SIGNALS.values()}
    verdict = PR7R_QUESTIONS["Q2"]["verdict"]
    if kinds == {"unproven"}:
        assert verdict == "blocked", kinds
    elif kinds == {"covered"}:
        assert verdict == "answered", kinds
    else:
        assert verdict == "open", kinds


def test_every_pr7r_test_agrees_with_the_catalog_about_its_scenario_id() -> None:
    """HFR-192: the id in a docstring is the id in the catalog, or neither is real.

    The regression this pins is the one review caught by hand: a test whose
    docstring opened ``HFR-186`` while the catalog filed it under ``HFR-187``,
    and two more that carried an id no catalog row claimed at all. Both failures
    are invisible to every other guard here -- the matrix checks that citations
    resolve to test FUNCTIONS, and nothing checked the reverse direction, that a
    test announcing a scenario id is the test that id was assigned to. A
    docstring id is how a reader navigates from a failure back to the scenario,
    so a wrong one is worse than none.

    Scope is the two PR7R modules on purpose: this asserts a convention the rest
    of the suite does not yet follow, and widening it is a separate change. What
    is NOT narrowed is which nodes count: the first draft walked ``tree.body``
    for ``ast.FunctionDef`` only, so an ``async def test_*`` -- the natural shape
    for the next probe in a unit whose subject is an async admission path -- or a
    test method inside a class would have been skipped in silence while the
    guard's own name promised every test. A guard that exempts the tests most
    likely to be written next is the fourth degenerate-assertion instance in this
    unit, so the walk recurses into classes and accepts both function kinds; the
    node id it builds carries the class components, exactly as pytest would.
    """
    yaml = pytest.importorskip("yaml")
    catalog_path = (
        Path(__file__).resolve().parent
        / "scenarios"
        / "harness_failure_recovery"
        / "catalog.yaml"
    )
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    scenarios = catalog["scenarios"] if isinstance(catalog, dict) else catalog
    by_id = {row["id"]: row["test"] for row in scenarios}

    modules = (
        "tests/test_run_terminal_truth_matrix.py",
        "tests/test_run_terminal_truth_evidence_probes.py",
    )
    def _tests(body: list[ast.stmt], prefix: tuple[str, ...]) -> list[tuple[str, ast.stmt]]:
        """Every test callable in ``body``, named the way pytest would name it."""
        found: list[tuple[str, ast.stmt]] = []
        for node in body:
            if isinstance(node, ast.ClassDef):
                found.extend(_tests(node.body, prefix + (node.name,)))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    found.append(("::".join(prefix + (node.name,)), node))
        return found

    repo_root = Path(__file__).resolve().parents[1]
    seen = 0
    for rel in modules:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
        for suffix, node in _tests(tree.body, ()):
            node_id = f"{rel}::{suffix}"
            doc = ast.get_docstring(node) or ""
            head = doc.split(":", 1)[0].split("/", 1)[0].strip()
            assert head.startswith("HFR-"), (
                f"{node_id} does not open its docstring with a scenario id"
            )
            assert head in by_id, f"{node_id} claims {head}, which no catalog row owns"
            assert by_id[head] == node_id, (
                f"{head} is filed against {by_id[head]} but claimed by {node_id}"
            )
            seen += 1
    # A floor, so deleting every docstring id cannot turn this into a vacuous pass.
    assert seen >= 10, seen


_PLAN = Path(__file__).resolve().parents[1] / "docs" / "plans" / "harness-run-reliability.md"


def test_the_plan_states_the_same_question_verdicts_as_the_matrix() -> None:
    """HFR-193: the plan's verdict list and ``PR7R_QUESTIONS`` cannot disagree.

    The regression this pins: round 6 moved Q2 to ``answered`` and Q5 to
    ``open`` in the matrix and in §7's narration, and left §7's own numbered
    "Question verdicts" block still saying Q2 blocks the inactivity timeout and
    Q5 is answered. The plan is the contract the next implementation unit reads,
    so a stale verdict there is worse than a stale comment -- it hands that unit
    the opposite instruction, in the document that is supposed to be
    authoritative, while every test stays green.

    Only the verdict WORD is tied. The prose either side of it is where the
    reasoning lives and is deliberately not machine-checked; what must not drift
    is the one token an implementer greps for.
    """
    text = _PLAN.read_text(encoding="utf-8")
    stated = dict(
        re.findall(r"^\d+\. \*\*(Q\d) [—-] (answered|open|blocked)\b", text, re.MULTILINE)
    )
    assert set(stated) == set(PR7R_QUESTIONS), stated
    for key, verdict in stated.items():
        assert verdict == PR7R_QUESTIONS[key]["verdict"], (
            f"{key}: plan says {verdict!r}, matrix says {PR7R_QUESTIONS[key]['verdict']!r}"
        )


def test_the_plans_reserved_scenario_range_is_actually_free() -> None:
    """HFR-194: a reserved id block may not contain ids the catalog already owns.

    The regression this pins: the round-6 commit added catalog rows HFR-188 to
    HFR-192 and left the plan's allocation summary advertising HFR-188…219 as
    reserved. That is the one line a follow-up unit reads to pick its ids, so the
    next probe would have been filed under an id this unit already owns -- and
    scenario ids are stable references, so a collision is not a rename away from
    being fixed.

    Both halves are checked, because either alone is satisfiable by cheating:
    every PR7R id must fall inside the occupied range, and no catalog id
    anywhere may fall inside the reserved one.
    """
    yaml = pytest.importorskip("yaml")
    catalog_path = (
        Path(__file__).resolve().parent / "scenarios" / "harness_failure_recovery" / "catalog.yaml"
    )
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    scenarios = catalog["scenarios"] if isinstance(catalog, dict) else catalog

    line = re.search(
        r"^- PR7: `HFR-(\d+)…(\d+)`.*?`HFR-(\d+)…(\d+)` still\s+reserved",
        _PLAN.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert line is not None, "the PR7 allocation line is not in the shape this guard reads"
    occupied = range(int(line.group(1)), int(line.group(2)) + 1)
    reserved = range(int(line.group(3)), int(line.group(4)) + 1)
    assert occupied.stop == reserved.start, (occupied, reserved)

    pr7r_modules = {
        "tests/test_run_terminal_truth_matrix.py",
        "tests/test_run_terminal_truth_evidence_probes.py",
    }
    ours = {
        int(row["id"].removeprefix("HFR-"))
        for row in scenarios
        if row["test"].split("::")[0] in pr7r_modules
    }
    assert ours, "no catalog row points at a PR7R module"
    assert ours <= set(occupied), sorted(ours - set(occupied))
    taken = {int(row["id"].removeprefix("HFR-")) for row in scenarios}
    assert not (taken & set(reserved)), sorted(taken & set(reserved))


_WORD_COUNTS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "all six": 6,
}
_CELL_COUNT_CLAIM = re.compile(
    r"\b(one|two|three|four|five|six|all six)\s+cells?\s+(?:are\s+|is\s+)?(covered|open)\b",
    re.IGNORECASE,
)


def test_no_prose_states_a_q2_cell_count_the_table_disagrees_with() -> None:
    """HFR-196: a sentence counting Q2's covered cells must count the real table.

    The regression this pins is round 8's second finding, and it is round 7's
    lesson recurring one artefact over. ``HFR-183``'s docstring summary still
    read "the durable Workbench lane has the signal; direct IM mostly does not"
    and its body still explained "4 cells covered, 2 open" -- two rounds
    after the same test's own assertions began requiring all six to be
    ``covered``. Nothing caught it: ``HFR-192`` reads only the scenario id out of
    a docstring, and the assertions and the prose describing them sat in the same
    function disagreeing with each other.

    The narrowness is deliberate and is the honest description of this guard: it
    does not police prose. It ties exactly one sentence SHAPE -- a spelled-out
    number of cells said to be covered or open -- to the table, because that is
    the shape that went stale, in a document and in a docstring, in successive
    rounds. Anything a summary line asserts in other words is still on the
    reader.
    """
    covered = sum(1 for kind, _ in EXACT_TURN_PROGRESS_SIGNALS.values() if kind == "covered")
    actual = {"covered": covered, "open": len(EXACT_TURN_PROGRESS_SIGNALS) - covered}

    repo_root = Path(__file__).resolve().parents[1]
    sources = (
        "tests/test_run_terminal_truth_matrix.py",
        "tests/test_run_terminal_truth_evidence_probes.py",
        "tests/run_terminal_truth_evidence.py",
        "docs/plans/harness-run-reliability.md",
    )
    checked = 0
    for rel in sources:
        # Backticks are markup around the same word, not part of the claim.
        text = (repo_root / rel).read_text(encoding="utf-8").replace("`", "")
        for word, kind in _CELL_COUNT_CLAIM.findall(text):
            checked += 1
            assert _WORD_COUNTS[word.lower()] == actual[kind.lower()], (
                f"{rel} says {word} cells {kind}; the table has "
                f"{actual[kind.lower()]}"
            )
    assert checked, "no cell-count claim found at all -- this guard is asserting nothing"
