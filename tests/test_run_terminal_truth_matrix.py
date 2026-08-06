"""HFR-184..187, HFR-189..190, HFR-192..194, HFR-196, HFR-198, HFR-200..201: the PR7R matrix is closed.

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
    RETRACTED_PHRASINGS,
    RETRACTION_MARKERS,
    RUN_TERMINAL_TRUTH_MATRIX,
    TRIGGERS,
    UNPROVEN_BUDGET,
)

_PROOF_KINDS = {"covered", "shared", "N/A", "defect", "unproven"}


_CELL_KEYS = {"shared", "per_backend"}


def _validate_matrix(matrix: dict) -> None:
    """Every key a cell can be looked up by must be one the expansion reads.

    Round 11. The expansion resolves a backend override with
    ``cell.get("per_backend", {}).get(backend, cell["shared"])``, and ``.get``
    with a default is a silent fallback: ``per_backend={"codecs": ...}`` drops
    the override for all three real backends and every downstream check --
    product size, ``UNPROVEN_BUDGET``, the per-cell assertions -- still passes,
    because a shared proof was substituted for evidence someone wrote
    specifically. That is the exact silent omission this matrix exists to
    prevent, committed by the matrix's own reader.

    The same hole exists one level up for a misspelled cell key: ``per_backends``
    would be ignored wholesale. So the check is a WHITELIST on both -- unknown
    keys are an error, not a no-op -- rather than a spell-check of the two
    names we happened to think of.

    Raises rather than asserts because ``_expand()`` runs at import time; a
    ``ValueError`` names the offending key instead of failing collection with a
    ``KeyError`` somewhere downstream, and it keeps the rule callable on a
    synthetic matrix from a regression test.
    """
    for (lane, trigger), rows in matrix.items():
        for outcome, cell in rows.items():
            where = f"({lane}, {trigger}) / {outcome}"
            unknown = set(cell) - _CELL_KEYS
            if unknown:
                raise ValueError(
                    f"{where}: unknown cell key(s) {sorted(unknown)}; the "
                    f"expansion reads only {sorted(_CELL_KEYS)}, so anything "
                    f"else is evidence written and never read"
                )
            if "shared" not in cell:
                raise ValueError(f"{where}: no ``shared`` proof to fall back to")
            stray = set(cell.get("per_backend", {})) - set(BACKENDS)
            if stray:
                raise ValueError(
                    f"{where}: per_backend names {sorted(stray)}, which is not "
                    f"in BACKENDS {sorted(BACKENDS)}; that override would be "
                    f"dropped and the shared proof used for every real backend"
                )


def _expand() -> list[tuple[str, str, str, str, tuple[str, str]]]:
    """The full backend x lane x trigger x outcome product.

    A cell written once for the lane applies to every backend; the expansion is
    what makes that a claim about all three rather than a silent omission.
    """
    _validate_matrix(RUN_TERMINAL_TRUTH_MATRIX)
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


def _assert_symbol_exists(qualified: str) -> tuple[list[ast.stmt], list[ast.AST]]:
    """Resolve a COMPLETE ``path::Class::symbol`` id, one nesting level at a time.

    Returns the module's own top-level body and the whole resolved CHAIN,
    innermost last, so a caller with a stricter contract than "the symbol
    exists" can check the kind of what it resolved to -- and, since round 10,
    the kinds it resolved THROUGH. Returning only the leaf was the same
    displacement one more level out: the leaf rule got tightened and the classes
    on the way to it stayed unexamined. The module body comes back with it
    because collectibility is not a property of the class node alone: round 13
    made a TestCase base something to RESOLVE, and resolving it needs the
    module's imports and its sibling classes.

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

    module_body: list[ast.stmt] = ast.parse(source.read_text(encoding="utf-8")).body
    scope: list[ast.stmt] = module_body
    chain: list[ast.AST] = []
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
        chain.append(match)
        scope = match.body
    return module_body, chain


#: The unittest base classes pytest's own plugin claims. Spelled out rather
#: than suffix-matched, for the reason in ``_unittest_ancestry``.
_UNITTEST_BASES = frozenset(
    {"TestCase", "IsolatedAsyncioTestCase", "FunctionTestCase"}
)


def _root_name(node: ast.expr) -> str:
    """The leftmost identifier of a dotted expression, or ``""``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _unittest_names(module_body: list[ast.stmt]) -> tuple[frozenset[str], frozenset[str]]:
    """Names bound in this module to a unittest TestCase, and to unittest itself.

    Import-aware because the alternative is name-shaped guessing, which is what
    round 13 caught. ``from unittest import IsolatedAsyncioTestCase as Base``
    binds a collectible base under a name with no "TestCase" in it, and
    ``class FakeTestCase`` binds a non-collectible one under a name that has it;
    only the import statement distinguishes them.
    """
    direct: set[str] = set()
    packages: set[str] = set()
    for node in module_body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "unittest" or alias.name.startswith("unittest."):
                    packages.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] != "unittest":
                continue
            for alias in node.names:
                if alias.name in _UNITTEST_BASES:
                    direct.add(alias.asname or alias.name)
                else:
                    packages.add(alias.asname or alias.name)
    return frozenset(direct), frozenset(packages)


def _unittest_ancestry(
    node: ast.ClassDef, module_body: list[ast.stmt], seen: frozenset[str] = frozenset()
) -> bool:
    """Does this class REALLY inherit a unittest TestCase, resolved in this module?

    Round 13's finding, and it is round 12's own fix read one step too
    literally. The rule was "a base whose trailing attribute name ends in
    ``TestCase``", which is a claim about spelling, not about ancestry:
    ``class Helper(FakeTestCase)`` passed it while pytest collects nothing from
    ``Helper``, and because BOTH readers of this predicate go through it, a
    catalog row naming a test inside such a class was discovered by the corpus
    walk and accepted by the citation check while never running.

    So the base is resolved instead of matched. An exact unittest name reached
    through the module's own imports counts -- ``unittest.TestCase``,
    ``from unittest import IsolatedAsyncioTestCase``, either under an alias --
    and a base defined in this module is followed transitively, which is the
    intermediate-base case the old docstring said it could not do. Anything the
    module does not define and did not import from unittest is NOT collectible
    here: a false rejection is loud and one line to fix, a false acceptance is
    the silent citation rot this resolver exists to stop.
    """
    direct, packages = _unittest_names(module_body)
    local = {n.name: n for n in module_body if isinstance(n, ast.ClassDef)}
    for base in node.bases:
        if isinstance(base, ast.Attribute):
            if base.attr in _UNITTEST_BASES and _root_name(base) in packages:
                return True
        elif isinstance(base, ast.Name):
            if base.id in direct:
                return True
            parent = local.get(base.id)
            if (
                parent is not None
                and base.id not in seen
                and _unittest_ancestry(parent, module_body, seen | {base.id})
            ):
                return True
    return False


def _test_flag(body: list[ast.stmt], attribute_of: str | None = None) -> bool | None:
    """The static ``__test__`` value stated in ``body``, or ``None`` if it says nothing.

    ``attribute_of`` looks for the OTHER spelling -- ``test_fn.__test__ = False``
    written in the scope that defines ``test_fn`` -- because pytest reads one
    attribute and does not care which statement set it.

    Only a literal counts. A computed ``__test__`` is not statically knowable,
    and reporting "no opinion" for it lands on the name rule, which is the
    conservative side: a false rejection is loud, a false acceptance is silent.
    """
    for node in body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign) and node.value is not None
            else []
        )
        for target in targets:
            named = (
                isinstance(target, ast.Name)
                and target.id == "__test__"
                and attribute_of is None
            ) or (
                isinstance(target, ast.Attribute)
                and target.attr == "__test__"
                and attribute_of is not None
                and _root_name(target) == attribute_of
            )
            if named and isinstance(node.value, ast.Constant):
                return bool(node.value.value)
    return None


def _collectible_class(node: ast.ClassDef, module_body: list[ast.stmt]) -> bool:
    """pytest's own default rule, read off the AST: ``Test*`` or a TestCase base.

    With no ``python_classes`` override in this repo, pytest collects a class
    only if its name starts with ``Test`` or the unittest plugin claims it as a
    ``TestCase`` subclass. The second half is decided by ``_unittest_ancestry``,
    which resolves the base rather than reading its name.

    Round 14 puts ``__test__`` in front of both, and it is BIDIRECTIONAL --
    checked against this repo's pytest, not reasoned, because the finding named
    only the opt-out and encoding half a rule is how this predicate has been
    wrong three rounds running. ``__test__ = False`` excludes the class whatever
    its name and whatever it inherits, unittest ancestry included.
    ``__test__ = True`` includes one whose name says nothing -- ``class Helper``
    with the flag really is collected -- and does NOT excuse it from the
    constructor rule, which refuses ``Test``-named and flag-opted-in classes
    alike.
    """
    flag = _test_flag(node.body)
    if flag is False:
        return False
    if _unittest_ancestry(node, module_body):
        # The unittest plugin claims these, constructor and all: TestCase
        # itself defines ``__init__``, so the rule below cannot apply here.
        return True
    if not (node.name.startswith("Test") or flag):
        return False
    # Round 12, verified against this repo's pytest rather than reasoned: a
    # name-collected class that defines ``__init__`` or ``__new__`` is REFUSED
    # with ``PytestCollectionWarning: cannot collect test class ... because it
    # has a __init__ constructor``, and its tests never run. Believing the name
    # alone would let a catalog row cite a green-looking scenario that pytest
    # skips in silence -- the exact rot this resolver exists to stop, one
    # attribute deeper than round 11 looked.
    return not any(
        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name in ("__init__", "__new__")
        for child in node.body
    )


def _collected_tests(
    body: list[ast.stmt],
    prefix: tuple[str, ...] = (),
    module_body: list[ast.stmt] | None = None,
) -> list[tuple[str, ast.stmt]]:
    """Every test callable pytest would collect from ``body``, named as pytest names it.

    Module level in round 11, and applying ``_collectible_class``, because the
    walker that discovers this unit's tests and the resolver that validates a
    cited node id are two readings of ONE rule -- "what does pytest collect" --
    and only the resolver got the rule. A ``class Helper`` with a ``test_x``
    method was walked into unconditionally here, so its method was reported as a
    discovered node; adding a catalog row for it then satisfied BOTH directions
    of the docstring/catalog tie while pytest collected nothing.

    That is round 10's lesson at the next joint out. Round 10 found a rule fixed
    at one nesting LEVEL that did not travel to the levels above it, and fixed
    it inside one function. The same rule then failed to travel to the other
    CALL SITE. A predicate that encodes an external system's behaviour belongs in
    one place with every reader going through it; two readers and one predicate
    is the shape that produced both bugs.
    """
    # Round 13: the top-level call IS the module, and nested calls carry it
    # down, because resolving a base to unittest needs the module's imports and
    # its sibling class definitions -- neither of which is visible from a class
    # body.
    module = body if module_body is None else module_body
    # Round 14, and it is round 10's lesson at the OUTERMOST level: a module
    # that sets ``__test__ = False`` is skipped whole, so every id in it is a
    # citation to something that never runs. Checked here rather than reasoned
    # about -- this repo's pytest collects nothing from such a file, not even a
    # bare ``def test_top``.
    if module_body is None and _test_flag(body) is False:
        return []
    found: list[tuple[str, ast.stmt]] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            if _collectible_class(node, module):
                found.extend(
                    _collected_tests(node.body, prefix + (node.name,), module)
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # The same bidirectional flag one scope out: ``test_fn.__test__``
            # is written by the enclosing body, so that is where it is read.
            flag = _test_flag(body, attribute_of=node.name)
            if flag is False:
                continue
            if node.name.startswith("test_") or flag:
                found.append(("::".join(prefix + (node.name,)), node))
    return found


def _assert_node_exists(node_id: str) -> None:
    """A pytest node id: the same resolution, plus what makes it a NODE id.

    The shared resolver deliberately accepts a class leaf, because a finding's
    OWNER may be a type. A pytest node id is a different thing: it has to be
    something pytest will collect and run. Resolving one with the owner rule
    left ``tests/foo.py::_helper`` and ``tests/foo.py::SomeClass`` green -- a
    citation that names a real symbol which no test run ever executes, which is
    the same failure this whole guard exists to prevent, one level in. So the
    leaf must be a function pytest collects, and the class-leaf latitude stays
    where it was justified.

    Round 10 finished the thought. Round 9 tightened the LEAF and left every
    class on the path to it judged by "a class with this name exists", so
    ``tests/foo.py::Helper::test_case`` resolved even though pytest collects
    neither ``Helper`` nor anything inside it. Same defect, same file, one
    nesting level out -- which is why the resolver now returns the chain and
    every non-leaf component is checked against pytest's collection rule.
    """
    assert node_id.split("::")[0].startswith("tests/"), node_id
    module_body, chain = _assert_symbol_exists(node_id)
    *containers, leaf = chain
    assert _test_flag(module_body) is not False, (
        f"{node_id}: the module sets ``__test__ = False``, so pytest skips the "
        f"whole file and nothing in it runs"
    )
    for container in containers:
        assert isinstance(container, ast.ClassDef) and _collectible_class(
            container, module_body
        ), (
            f"{node_id}: pytest does not collect class {container.name!r} "
            f"(not Test*-named, no resolved unittest TestCase base, or opted out "
            f"with ``__test__``), so nothing inside it runs"
        )
    assert isinstance(leaf, (ast.FunctionDef, ast.AsyncFunctionDef)), (
        f"{node_id}: a node id must name a test function, not a "
        f"{type(leaf).__name__.removesuffix('Def').lower()}"
    )
    # The leaf's own ``__test__``, read from the scope that would have set it.
    enclosing = containers[-1].body if containers else module_body
    leaf_flag = _test_flag(enclosing, attribute_of=leaf.name)
    assert leaf_flag is not False, (
        f"{node_id}: {leaf.name!r} is opted out with ``__test__ = False``"
    )
    assert leaf.name.startswith("test_") or leaf_flag, (
        f"{node_id}: {leaf.name!r} is not collected by pytest"
    )


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
    #
    # Round 10: the comment above said "per (lane, outcome)" and the assertion
    # counted globally, so fourteen distinct probes all describing the SUCCESS
    # row satisfied it while thirteen rows said nothing of their own. This unit
    # has now produced that exact shape -- an assertion that reads like the
    # comment above it and enforces something weaker -- five times, and it is
    # the fifth one appearing in the very assertion written to catch the first
    # four. So the grouping is real now: each (lane, outcome) that still has a
    # gap must name at least one probe no other group is already claiming.
    groups: dict[tuple[str, str], set[str]] = {}
    for _b, lane_key, _t, outcome_key, (kind, detail) in _CELLS:
        if kind == "unproven":
            groups.setdefault((lane_key, outcome_key), set()).add(detail)
    for group, details in sorted(groups.items()):
        elsewhere = {d for other, ds in groups.items() if other != group for d in ds}
        assert details - elsewhere, (
            f"{group}: every unproven cell here reuses a probe named for another "
            f"(lane, outcome) -- this row states no gap of its own"
        )


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
    """HFR-192: the id in a docstring is the id in the catalog, and back again.

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
    repo_root = Path(__file__).resolve().parents[1]
    discovered: set[str] = set()
    for rel in modules:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))
        for suffix, node in _collected_tests(tree.body):
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
            discovered.add(node_id)
    # A floor, so deleting every docstring id cannot turn this into a vacuous pass.
    assert len(discovered) >= 10, len(discovered)

    # And the other direction, which round 10 found missing. Everything above
    # walks FROM the tests: delete a probe, or rename it, and its catalog row
    # keeps saying ``status: covered`` against a node id nothing collects, with
    # this guard green -- the floor of ten is far too coarse to notice one
    # scenario going dark. The catalog is what the plan and the next unit read
    # to decide what is already proven, so a row pointing at a deleted test is
    # a false claim of coverage, not a stale comment. Scope stays the two PR7R
    # modules for the same reason it does above: rows filed against the rest of
    # the suite follow a convention this unit has not audited.
    orphans = sorted(
        (row["id"], row["test"])
        for row in scenarios
        if row["test"].split("::")[0] in modules and row["test"] not in discovered
    )
    assert not orphans, f"catalog rows pointing at tests that no longer exist: {orphans}"
    assert len({row["id"] for row in scenarios if row["test"].split("::")[0] in modules}) == len(
        discovered
    ), "every PR7R test owns exactly one catalog row and vice versa"


def test_one_scenario_id_names_exactly_one_catalog_row() -> None:
    """HFR-202: a scenario id is a stable name, so two rows may not answer to it.

    Round 12's finding, and it is the set-collapse defect one layer above the
    ones rounds 10 and 11 found. Every check in HFR-192 reads the catalog
    through ``{row["id"]: row["test"]}`` or compares ``{ids}`` against
    ``discovered`` -- both of which SHRINK a duplicate to one element. So two
    rows carrying the same id pass the count, the tie and the orphan check,
    while ``by_id`` keeps whichever came last and every other consumer keeps
    whichever it happened to read. The catalog is the canonical record the plan
    and the next unit read to decide what is proven; two canonical definitions
    for one stable id is not a formatting problem, it is the record disagreeing
    with itself, and the disagreement is invisible precisely because the
    de-duplication happens before anything looks.

    Scope is the WHOLE catalog rather than the PR7R modules. Everywhere else
    this unit narrows to its own rows because it is asserting a convention the
    rest of the suite has not adopted; uniqueness of a primary key is not a
    convention, and ``by_id`` is built from every row regardless of scope.

    What is deliberately NOT asserted: that a test is cited once. Seven tests
    in this catalog legitimately prove more than one scenario, and a guard that
    banned that would be a guard someone turns off.
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

    seen: dict[str, dict] = {}
    collisions: list[str] = []
    for row in scenarios:
        first = seen.setdefault(row["id"], row)
        if first is not row:
            same = "identical" if first == row else "CONFLICTING"
            collisions.append(
                f"{row['id']}: two rows ({same}) -- first cites "
                f"{first.get('test')!r}, second cites {row.get('test')!r}"
            )
    assert not collisions, "\n".join(collisions)
    # A floor, so an empty or mis-parsed catalog cannot pass this vacuously.
    assert len(seen) == len(scenarios) >= 200, (len(seen), len(scenarios))


_PLAN = Path(__file__).resolve().parents[1] / "docs" / "plans" / "harness-run-reliability.md"

_PLAN_VERDICT = re.compile(
    r"^\d+\. \*\*(Q\d) [—-] (answered|open|blocked)\b", re.MULTILINE
)


def _stated_plan_verdicts(text: str) -> dict[str, str]:
    """The plan's numbered verdict block, refusing to collapse a duplicate.

    Round 13, and the same defect as round 12's catalog finding one document
    over: ``dict(re.findall(...))`` keeps the LAST pair for a repeated key, so a
    stale ``Q2 — open`` line left above the current ``Q2 — answered`` line
    vanishes into the mapping. The key-set check and the verdict check then both
    pass while §7 still hands the next implementation unit two contradictory
    instructions -- which is precisely the drift HFR-193 was written to stop, so
    the guard would have been reporting agreement about a document that
    disagrees with itself.

    Raising rather than asserting keeps the rule callable on synthetic text, so
    the regression test does not have to corrupt the real plan to prove the
    guard bites.
    """
    pairs = _PLAN_VERDICT.findall(text)
    stated: dict[str, str] = {}
    for question, verdict in pairs:
        if question in stated:
            raise ValueError(
                f"{question}: the plan states its verdict more than once "
                f"({stated[question]!r} then {verdict!r}); a reader greps and "
                f"stops at the first one"
            )
        stated[question] = verdict
    return stated


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

    Round 13 adds the duplicate rule (see ``_stated_plan_verdicts``): one
    question may state its verdict once. The fixtures below are checked before
    the real plan, because a guard whose parse silently de-duplicates is not
    checking the document it reports on.
    """
    good = "1. **Q1 — open.** x\n2. **Q2 — answered.** y\n"
    assert _stated_plan_verdicts(good) == {"Q1": "open", "Q2": "answered"}
    with pytest.raises(ValueError, match="Q2: the plan states its verdict more"):
        _stated_plan_verdicts(good + "3. **Q2 — open.** the stale copy\n")

    text = _PLAN.read_text(encoding="utf-8")
    stated = _stated_plan_verdicts(text)
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


def test_a_node_id_citation_must_name_something_pytest_collects(tmp_path, monkeypatch) -> None:
    """HFR-198: the citation resolver's leaf rule, checked at both strictnesses.

    Round 9's fourth finding, and it is the round-7 lesson again in its own
    house: a guard's EXEMPTIONS are invisible, and this one had an exemption it
    never re-justified. ``_assert_symbol_exists`` accepts a class leaf on
    purpose, because a finding's OWNER may be a type. ``_assert_node_exists``
    reused it verbatim, so a node id naming a private helper or a bare class
    resolved happily -- a citation pointing at a real symbol that no test run
    ever executes, which is exactly the failure mode the resolver was written to
    prevent, displaced one level.

    Both directions are asserted, because tightening the leaf rule everywhere
    would have been the easy over-correction and would have broken the owner
    citations that legitimately name classes.

    Round 10 extended it one level out and the extension is the point of the
    scenario now: round 9 fixed the leaf and left the CONTAINER unjudged, so
    ``Helper::test_case`` -- a class pytest never collects, holding a function
    named exactly like a test -- still resolved. The lesson this unit keeps
    relearning is that a rule fixed at one nesting level does not travel; the
    corpus below therefore names a collectible container, an uncollectible one,
    and the same leaf under both.

    Round 14 adds the one input that outranks every rule above it: ``__test__``.
    Each level had been decided from the NAME, and pytest lets a file, a class
    or a function overrule its own name in either direction. The fixture pins
    all six combinations, because the tempting single-line reading -- "if the
    flag is set, believe it" -- is wrong twice: a flagged-in class with a
    constructor is still refused, and a flagged-out module takes everything
    inside it down with it.
    """
    cited = {
        detail if kind == "covered" else _detail_node(detail)
        for *_dims, (kind, detail) in _CELLS
        if kind in {"covered", "shared", "defect"}
    }
    cited.update(
        node_id for entry in PR7R_QUESTIONS.values() for node_id in entry["evidence"]
    )
    cited.update(finding["reproducer"] for finding in PR7R_FINDINGS.values())

    module = tmp_path / "tests" / "sample_module.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "import unittest\n"
        "from unittest import IsolatedAsyncioTestCase as Base\n"
        "class FakeTestCase:\n"
        "    pass\n"
        "class Helper(FakeTestCase):\n"
        "    def test_case(self): ...\n"
        "class AliasedTests(Base):\n"
        "    async def test_case(self): ...\n"
        "class Intermediate(unittest.TestCase):\n"
        "    pass\n"
        "class Derived(Intermediate):\n"
        "    def test_case(self): ...\n"
        "class TestOptOut:\n"
        "    __test__ = False\n"
        "    def test_case(self): ...\n"
        "class OptOutTests(unittest.TestCase):\n"
        "    __test__ = False\n"
        "    def test_case(self): ...\n"
        "class FlaggedIn:\n"
        "    __test__ = True\n"
        "    def test_case(self): ...\n"
        "class TestFlaggedCtor:\n"
        "    __test__ = True\n"
        "    def __init__(self): ...\n"
        "    def test_case(self): ...\n"
        "class Owner:\n"
        "    def method(self): ...\n"
        "    def test_case(self): ...\n"
        "class TestGood:\n"
        "    def test_case(self): ...\n"
        "class OwnerTests(unittest.IsolatedAsyncioTestCase):\n"
        "    async def test_case(self): ...\n"
        "class TestCtor:\n"
        "    def __init__(self): ...\n"
        "    def test_case(self): ...\n"
        "class TestNew:\n"
        "    def __new__(cls): ...\n"
        "    def test_case(self): ...\n"
        "class CtorTests(unittest.IsolatedAsyncioTestCase):\n"
        "    def __init__(self, *a, **kw): super().__init__(*a, **kw)\n"
        "    async def test_case(self): ...\n"
        "def _helper(): ...\n"
        "def test_real(): ...\n"
        "async def test_async_real(): ...\n"
        "def test_muted(): ...\n"
        "test_muted.__test__ = False\n"
        "def plain_named(): ...\n"
        "plain_named.__test__ = True\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rel = Path("tests/sample_module.py")

    # The owner resolver: a class leaf and a method leaf are both legitimate,
    # and it stays indifferent to whether the container is collectible -- an
    # owner is a place to change code, not a thing pytest runs.
    for owner in (f"{rel}::Owner", f"{rel}::Owner::method", f"{rel}::_helper"):
        assert _assert_symbol_exists(owner)[1], owner

    # The node-id resolver: only a collected test function, under a collected
    # class if it is nested at all.
    for node_id in (
        f"{rel}::test_real",
        f"{rel}::test_async_real",
        f"{rel}::TestGood::test_case",
        f"{rel}::OwnerTests::test_case",
    ):
        _assert_node_exists(node_id)
    for rejected in (f"{rel}::Owner", f"{rel}::_helper"):
        with pytest.raises(AssertionError):
            _assert_node_exists(rejected)
    with pytest.raises(AssertionError, match="pytest does not collect class 'Owner'"):
        # The leaf is a perfectly good test function; the container is not a
        # test class, so pytest never reaches it.
        _assert_node_exists(f"{rel}::Owner::test_case")

    # Round 11: the OTHER reader of the same rule. ``_collected_tests`` walks
    # these modules to discover what has a docstring id, and it recursed into
    # every class unconditionally -- so ``Owner::test_case`` was reported as a
    # discovered node, and adding a catalog row for it satisfied BOTH directions
    # of HFR-192 while pytest collected nothing. Discovery and citation are two
    # readings of one question and now share one predicate, which is checked
    # here on the same fixture rather than in two places that can drift.
    # Round 12 adds the constructor rule to the same fixture, on both readers.
    # ``TestCtor`` and ``TestNew`` are named exactly as pytest requires and are
    # still not collected -- this repo's pytest says so out loud
    # (``PytestCollectionWarning: cannot collect test class 'TestCtor' because
    # it has a __init__ constructor``) -- while ``CtorTests`` defines the same
    # constructor and IS collected, because the unittest plugin claims it. So
    # the rule is not "no constructor"; it is "no constructor on a class
    # collected by NAME", and asserting the third case is what keeps the fix
    # from being a blanket ban that quietly drops real tests.
    for node_id in (f"{rel}::CtorTests::test_case",):
        _assert_node_exists(node_id)
    for rejected_class in ("TestCtor", "TestNew"):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{rejected_class}'"
        ):
            _assert_node_exists(f"{rel}::{rejected_class}::test_case")

    # Round 13 turns the TestCase base from a SPELLING into an ancestry. The old
    # rule accepted any base whose trailing name ended in "TestCase", so
    # ``Helper(FakeTestCase)`` -- a plain class inheriting a plain class -- was
    # collectible on both readers, and a catalog row citing a test inside it
    # would have been discovered by the walk and accepted by the citation check
    # while pytest ran nothing. The fixture pins both directions of the
    # resolution, because tightening it to the literal names ``TestCase`` /
    # ``IsolatedAsyncioTestCase`` would have been the easy over-correction:
    # ``AliasedTests`` reaches the real base through an import alias with no
    # "TestCase" in its name, and ``Derived`` reaches it through an
    # intermediate the old docstring admitted it could not follow.
    for node_id in (f"{rel}::AliasedTests::test_case", f"{rel}::Derived::test_case"):
        _assert_node_exists(node_id)
    with pytest.raises(AssertionError, match="pytest does not collect class 'Helper'"):
        _assert_node_exists(f"{rel}::Helper::test_case")

    # Round 14: ``__test__``, which every rule above had been reasoning around.
    # It is not a second name rule -- it OVERRIDES the name in both directions,
    # and each direction was probed against this repo's pytest rather than
    # assumed. Opting out beats even unittest ancestry (``OptOutTests`` is a
    # real ``TestCase`` and pytest still skips it), while opting in admits a
    # class with no ``Test`` prefix at all (``FlaggedIn``). What it does NOT do
    # is excuse the constructor rule: ``TestFlaggedCtor`` sets the flag true and
    # is still refused, which is the case that would have turned a naive "flag
    # wins" reading into a discovery walk citing tests that never run.
    _assert_node_exists(f"{rel}::FlaggedIn::test_case")
    for opted_out in ("TestOptOut", "OptOutTests", "TestFlaggedCtor"):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{opted_out}'"
        ):
            _assert_node_exists(f"{rel}::{opted_out}::test_case")

    # The same flag on a FUNCTION, which is written one scope out from the
    # function it applies to -- pytest reads an attribute and does not care
    # which statement set it, so the resolver reads the enclosing body.
    _assert_node_exists(f"{rel}::plain_named")
    with pytest.raises(AssertionError, match="'test_muted' is opted out"):
        _assert_node_exists(f"{rel}::test_muted")

    discovered = {suffix for suffix, _node in _collected_tests(ast.parse(
        module.read_text(encoding="utf-8")
    ).body)}
    assert discovered == {
        "test_real",
        "test_async_real",
        "plain_named",
        "TestGood::test_case",
        "OwnerTests::test_case",
        "CtorTests::test_case",
        "AliasedTests::test_case",
        "Derived::test_case",
        "FlaggedIn::test_case",
    }, discovered
    for absent in (
        "Owner::test_case",
        "Helper::test_case",
        "TestOptOut::test_case",
        "OptOutTests::test_case",
        "TestFlaggedCtor::test_case",
        "test_muted",
    ):
        assert absent not in discovered, absent

    # And the outermost level, which is round 10's lesson yet again: a module
    # setting ``__test__ = False`` is skipped WHOLE, so a bare ``def test_top``
    # in it never runs. Discovery returns nothing and every citation into it is
    # rejected -- checked on a separate module because the flag is file-scoped
    # and would have silenced the fixture above.
    muted = tmp_path / "tests" / "muted_module.py"
    muted.write_text(
        "__test__ = False\n"
        "def test_top(): ...\n"
        "class TestInside:\n"
        "    def test_case(self): ...\n",
        encoding="utf-8",
    )
    muted_rel = Path("tests/muted_module.py")
    assert (
        _collected_tests(ast.parse(muted.read_text(encoding="utf-8")).body) == []
    )
    for node_id in (f"{muted_rel}::test_top", f"{muted_rel}::TestInside::test_case"):
        with pytest.raises(AssertionError, match="the module sets"):
            _assert_node_exists(node_id)

    # And the real citations still pass under the tightened rule -- the point of
    # a stricter guard is that the corpus already satisfies it, checked here
    # rather than left to the parametrized cells so a tightening that quietly
    # invalidated the corpus would fail in ONE place with the whole list.
    monkeypatch.undo()
    assert cited, "no citations to check"
    for node_id in sorted(cited):
        _assert_node_exists(node_id)


def test_a_mistyped_matrix_key_is_an_error_not_a_silent_fallback():
    """HFR-200: an override key the expansion cannot read must fail loudly.

    The expansion resolves a backend override with ``.get(backend, shared)``,
    and a defaulted lookup cannot tell "no override was written" from "an
    override was written under a name nobody reads". A cell carrying
    ``per_backend={"codecs": ...}`` loses that evidence for all three real
    backends, and every downstream check stays green: the product is still
    3 x 2 x 4 x 7, the unproven count is unchanged, and each surviving cell
    still names a probe. The matrix's stated purpose is that a cell written once
    per lane is a claim about all three backends rather than a silent omission,
    which makes a silently-dropped override the one failure it must not have.

    Same shape as the degenerate guards rounds 8-11 kept finding, in the reader
    rather than in an assertion: a lookup that cannot fail reports success for
    an input it never handled.
    """
    good = {
        (lane, trigger): {
            outcome: {"shared": ("unproven", "x"), "per_backend": {"codex": ("unproven", "y")}}
            for outcome in OUTCOMES
        }
        for lane in LANES
        for trigger in TRIGGERS
    }
    _validate_matrix(good)

    def _mutated(**cell):
        broken = {k: dict(v) for k, v in good.items()}
        first = next(iter(broken))
        broken[first] = dict(broken[first])
        broken[first][OUTCOMES[0]] = cell
        return broken

    with pytest.raises(ValueError, match=r"per_backend names \['codecs'\]"):
        _validate_matrix(_mutated(shared=("unproven", "x"), per_backend={"codecs": ("unproven", "y")}))

    with pytest.raises(ValueError, match=r"unknown cell key\(s\) \['per_backends'\]"):
        _validate_matrix(_mutated(shared=("unproven", "x"), per_backends={"codex": ("unproven", "y")}))

    with pytest.raises(ValueError, match="no ``shared`` proof"):
        _validate_matrix(_mutated(per_backend={"codex": ("unproven", "y")}))

    # And the checked-in matrix satisfies it, which is what makes the guard a
    # statement about this corpus rather than about a fixture.
    _validate_matrix(RUN_TERMINAL_TRUTH_MATRIX)


_LEDGER_LITERALS = frozenset(
    f'"{phrase}",' for phrase, _round, _why in RETRACTED_PHRASINGS
)


def _flatten(text: str) -> str:
    """One flat lowercase line, with the adjacent-literal join closed up."""
    flat = re.sub(r'"\s+"', "", text)  # adjacent literals: "...the " "rest..."
    return re.sub(r"\s+", " ", flat).strip().lower()


#: A YAML mapping key, with or without the sequence dash that may precede it.
_YAML_KEY = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][\w.-]*:(?:\s|$)")
_YAML_ITEM = re.compile(r"^\s*-\s")
#: The block-scalar indicators, which are syntax rather than prose.
_BLOCK_INDICATORS = frozenset({">", "|", ">-", "|-", ">+", "|+"})


def _yaml_prose_units(lines: list[str]) -> list[str]:
    """One unit per YAML scalar and per contiguous comment block.

    Round 14. A structured file is not continuous prose, and flattening it as if
    it were is what let a marker in one field vouch for a claim in another: the
    fields have no terminal punctuation, so ``name: ...`` and the ``detail:``
    six lines below it merged into a single "sentence" and the proximity rule
    scoped over both. The unit here is what an author actually writes as one
    statement -- a scalar value, however many lines the block folds over -- so a
    retraction has to sit in the same field as the claim it retracts.

    Comments are units in their own right rather than dropped: ``catalog.yaml``
    carries substantial prose in ``#`` blocks that ``yaml.safe_load`` would
    discard, and a guard that cannot see half the file is the failure this
    ledger exists to prevent. They are their own scope for the same reason the
    fields are -- a comment above a row does not retract what the row asserts.
    """
    units: list[str] = []
    buffer: list[str] = []
    in_comment = False

    def flush() -> None:
        nonlocal buffer, in_comment
        if buffer:
            joined = _flatten(" ".join(buffer))
            if joined:
                units.append(joined)
        buffer, in_comment = [], False

    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped in _LEDGER_LITERALS:
            flush()
            continue
        if stripped.startswith("#"):
            if not in_comment:
                flush()
                in_comment = True
            buffer.append(re.sub(r"^#+\s?", "", stripped))
            continue
        if in_comment:
            flush()
        if _YAML_KEY.match(raw):
            flush()
            value = stripped.split(":", 1)[1].strip()
            buffer = [] if value in _BLOCK_INDICATORS else [value]
        elif _YAML_ITEM.match(raw):
            flush()
            buffer = [stripped[1:].strip()]
        else:
            # A continuation line of the folded scalar opened above.
            buffer.append(stripped)
    flush()
    return units


def _prose_units(path: Path, span: tuple[int, int] | None = None) -> list[str]:
    """The searchable prose of one artefact, split into independent scopes.

    A phrase in this corpus is routinely split across a line break, wrapped in a
    ``#`` comment, or spread over two adjacent Python string literals, so a
    naive substring search over the raw file finds none of them -- which would
    make the ledger below a guard that passes because it cannot see. Python and
    Markdown are therefore still flattened whole: a wrapped comment or a folded
    docstring IS one continuous statement there, and cutting it at line
    boundaries would hide every phrase that spans one.

    YAML is not, for the reason in ``_yaml_prose_units``.

    The ledger's OWN row literals are dropped. Leaving them in would make every
    row trivially findable by its own definition, which is precisely how the
    "this row matches nothing" check would stop meaning anything.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if span is not None:
        lines = lines[span[0] : span[1]]
    lines = [line for line in lines if line.strip() not in _LEDGER_LITERALS]
    if path.suffix in {".yaml", ".yml"}:
        return _yaml_prose_units(lines)
    flat = _flatten(" ".join(re.sub(r"^#+\s?", "", line.strip()) for line in lines))
    return [flat] if flat else []


def _pr7r_plan_span(path: Path) -> tuple[int, int]:
    """Line bounds of the plan's PR7R block, which is the part this unit owns.

    Scoping matters: the plan discusses process replacement and delivery batches
    elsewhere in language that legitimately reuses these words, and a guard that
    fired on those would be turned off rather than obeyed.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("#### PR7R status"))
    end = next(
        (
            i
            for i, ln in enumerate(lines[start + 1 :], start + 1)
            if ln.startswith("### ") or ln.startswith("## ")
        ),
        len(lines),
    )
    return start, end


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _marker_near(prose: str, start: int, end: int) -> bool:
    """Is a retraction marker in the phrase's OWN sentence?

    The first draft of this rule used a 400-character window, and round 11's
    counter-check killed it: restoring the real stale sentence to
    ``observations.yaml`` left the guard GREEN, because the word "narrower" --
    describing the consequence of the gate split, nothing to do with any
    retraction -- sat inside the window. A proximity rule wide enough to span
    unrelated prose does not test proximity to a retraction, it tests prose
    density. That is the ninth degenerate guard this unit has found, and the
    first one it found in its own new code.

    The second draft allowed the following sentence too, for the "claim, full
    stop, THAT IS FALSE" shape -- and that draft passed the stale text as well,
    because "narrower" was in exactly that following sentence. So the
    scope is the phrase's own sentence and nothing else. The cost is real and
    accepted: an author who quotes a retracted claim and corrects it in the NEXT
    sentence must move the correction into the same one. The benefit is that the
    rule cannot be satisfied by neighbouring prose that is about something else.
    """
    bounds = [0, *(m.end() for m in _SENTENCE_BOUNDARY.finditer(prose)), len(prose)]
    spans = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    scope = "".join(
        prose[lo:hi] for lo, hi in spans if lo <= start < hi or lo < end <= hi
    )
    # Whole words, round 12. Substring matching let the stem "narrow" be
    # satisfied by "narrower", so one sentence asserting a banned claim and
    # using that adjective for anything at all passed the guard -- the rule
    # reproducing the accident it was written to catch, one round after naming
    # the very word. The fixtures in HFR-201 spell the case out.
    return any(
        re.search(rf"\b{re.escape(marker)}\b", scope) for marker in RETRACTION_MARKERS
    )


def test_no_retracted_phrasing_survives_outside_its_own_retraction(tmp_path):
    """HFR-201: a claim this unit retracted may only appear next to its retraction.

    Round 11's finding is that round 10 retracted the codex overlap claim in
    five artefacts and left it standing in the catalog -- the file that exists
    to BE the canonical record, so a follow-up unit reading only the scenario
    row would have taken the retracted contract as the contract. A seventh copy
    sat unnoticed in the round-9 observation.

    Rounds 7, 9 and 10 each found the same class -- a stale docstring, a stale
    headline range, a stale document copy -- and each was fixed as a text edit
    while the class was named only in prose. Naming a class does not enforce it;
    the recurrence is the evidence. So the ledger is data
    (``RETRACTED_PHRASINGS``) and this is its enforcement across every artefact
    the unit owns.

    The rule is deliberately narrow enough to be mechanical: a retracted
    phrasing may occur in a sentence that carries a retraction marker, or in the
    sentence right before one, and nowhere else (see ``_marker_near``, whose
    first draft was itself too loose to fail on the real stale text). Quoting an
    error to correct it is the point; restating it as an assertion is what
    recurred three times.

    Round 14 fixes the input rather than the rule: "its own sentence" was being
    computed over a whole flattened file, and a YAML field ends with no full
    stop, so several fields and the comment after them were one sentence and a
    marker in any of them vouched for all. The corpus is now searched per prose
    UNIT -- a scalar value or a comment block in YAML, the whole file in Python
    and Markdown, where a wrapped comment really is one statement. Three rounds
    have now narrowed this one guard (window, then whole words, now scope), each
    time because the previous width passed text it was written to fail.
    """
    repo_root = Path(__file__).resolve().parents[1]
    plan = repo_root / "docs" / "plans" / "harness-run-reliability.md"
    corpus: list[tuple[Path, tuple[int, int] | None]] = [
        (repo_root / "tests" / "run_terminal_truth_evidence.py", None),
        (repo_root / "tests" / "test_run_terminal_truth_matrix.py", None),
        (repo_root / "tests" / "test_run_terminal_truth_evidence_probes.py", None),
        (repo_root / "tests" / "scenarios" / "harness_failure_recovery" / "catalog.yaml", None),
        (
            repo_root / "tests" / "scenarios" / "harness_failure_recovery" / "observations.yaml",
            None,
        ),
        (plan, _pr7r_plan_span(plan)),
    ]
    for path, _span in corpus:
        assert path.exists(), path

    # Guard the guard, on the three shapes that decided its width. Only a marker
    # in the phrase's own sentence counts: the third string below is the REAL
    # stale sentence this round removed, and both looser drafts of the rule
    # passed it, rescued by "narrower" in the sentence after -- a word about the
    # consequence of the gate split, not about any retraction.
    # The fixtures are BUILT from the ledger row rather than spelled out, so
    # this file does not itself become a corpus offender -- and so a reworded
    # row keeps exercising the rule instead of silently testing a dead string.
    banned = RETRACTED_PHRASINGS[0][0]

    def _accepts(prose: str) -> bool:
        match = re.search(re.escape(banned), prose)
        assert match is not None, prose
        return _marker_near(prose, match.start(), match.end())

    assert _accepts(f'round 9 wrote "the two {banned}"; round 10 narrowed it.')
    assert not _accepts(f"the two {banned}. that is false: a window exists.")
    assert not _accepts(
        f"the two {banned}, and the mute turn was interrupted. the real "
        f"consequence of the split is narrower: it replaces rather than queues."
    )
    # Round 12: the same sentence, with the accident word moved INSIDE it. The
    # sentence rule alone does not save a substring marker -- "narrower"
    # contains "narrow" -- so the markers are whole words now, and this is the
    # case that says so.
    assert not _accepts(f"the two {banned} in the narrower gate.")

    # Round 14: the sentence rule is only as good as what counts as ONE piece of
    # prose. A YAML field ends with no full stop, so flattening the file joined
    # a stale ``name:`` to whatever field came next and the "own sentence" scope
    # spanned both -- a marker anywhere downstream vouching for a claim it has
    # nothing to do with. The fixture is a real two-field row, and both halves
    # matter: the stale field must be caught, and the neighbour that legitimately
    # carries the retraction must still be accepted.
    low = banned.lower()

    def _unrescued(units: list[str]) -> list[str]:
        return [
            unit
            for unit in units
            for m in [re.search(re.escape(low), unit)]
            if m is not None and not _marker_near(unit, m.start(), m.end())
        ]

    sample = tmp_path / "sample.yaml"
    sample.write_text(
        "scenarios:\n"
        "  - id: HFR-999\n"
        f"    name: the two {low}\n"
        "    detail: round 9 wrote that; round 10 retracted it\n"
        "# a trailing comment that merely says retracted\n",
        encoding="utf-8",
    )
    units = _prose_units(sample)
    assert f"the two {low}" in units, units
    assert _unrescued(units) == [f"the two {low}"], units
    # ...while a field that carries its own retraction passes, and a folded
    # block scalar is NOT chopped at its line breaks -- the over-correction
    # here would be to make every SOURCE line its own scope, which would hide
    # any phrase that wraps.
    folded = tmp_path / "folded.yaml"
    folded.write_text(
        "detail: >-\n"
        "  round 9 wrote that the two\n"
        f"  {low}; round 10 retracted it.\n",
        encoding="utf-8",
    )
    assert _prose_units(folded) == [
        f"round 9 wrote that the two {low}; round 10 retracted it."
    ]
    assert _unrescued(_prose_units(folded)) == []

    # A phrase that matches nothing is a ledger row that enforces nothing, and a
    # renamed subject would turn every row into one silently. Each row must
    # still be FINDABLE somewhere -- next to its retraction, which is where the
    # rule below then requires it to be.
    hits: dict[str, int] = {phrase: 0 for phrase, _round, _why in RETRACTED_PHRASINGS}
    offenders: list[str] = []
    for path, span in corpus:
        for prose in _prose_units(path, span):
            for phrase, round_name, why in RETRACTED_PHRASINGS:
                for match in re.finditer(re.escape(phrase.lower()), prose):
                    hits[phrase] += 1
                    if not _marker_near(prose, match.start(), match.end()):
                        offenders.append(
                            f"{path.name}: {phrase!r} was retracted in {round_name} "
                            f"but is stated here as fact -- {why}"
                        )
    assert not offenders, "\n".join(offenders)
    unfindable = sorted(phrase for phrase, count in hits.items() if not count)
    assert not unfindable, (
        f"ledger rows matching nothing in the corpus: {unfindable} -- either the "
        f"retraction narrative was deleted or the phrase no longer spells the "
        f"claim it bans"
    )
