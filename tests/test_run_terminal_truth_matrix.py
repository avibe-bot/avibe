"""HFR-184..187, HFR-189..190, HFR-192..194, HFR-196, HFR-198, HFR-200..201: the PR7R matrix is closed.

Same contract as HFR-105 for ``TEARDOWN_SETTLEMENT_MATRIX``: growing a
dimension fails here until every new cell names a consuming test or a precise
ownership reason. PR7R adds two obligations HFR-105 does not have -- every
named node id must resolve to a real test function, and the ``unproven`` count
must equal the checked-in budget so a gap cannot widen or be papered over
silently.
"""

import ast
import functools
import io
import re
import tokenize
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
    _TRIGGER_OVERRIDES,
    _validate_trigger_overrides,
)

_PROOF_KINDS = {"covered", "shared", "N/A", "defect", "unproven"}
_FINDING_ID = re.compile(r"\bPR7R-F\d+\b")
_SCENARIO_ID = re.compile(r"\bHFR-\d+\b")
_SCENARIO_CATALOG = (
    Path(__file__).resolve().parent
    / "scenarios"
    / "harness_failure_recovery"
    / "catalog.yaml"
)


_CELL_KEYS = {"shared", "per_backend"}


def _scenario_rows() -> list[dict]:
    yaml = pytest.importorskip("yaml")
    catalog = yaml.safe_load(_SCENARIO_CATALOG.read_text(encoding="utf-8"))
    return catalog["scenarios"] if isinstance(catalog, dict) else catalog


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


def _dotted_bindings(module_body: list[ast.stmt]) -> dict[str, str]:
    """Local name -> the dotted path it stands for, for ABSOLUTE imports only.

    Round 22. ``import a.b as x`` binds a module; ``from a.b import C`` binds
    whatever ``C`` is, which may be a class or may be a submodule -- the
    statement does not say, and neither does this map. The caller decides by
    trying to open the path, which is the only way to tell them apart without
    importing anything.

    Relative imports and ``import *`` are left out on purpose: neither can be
    resolved from this AST alone, and a name this map does not carry is a name
    ``_base_class`` reports as unresolvable, which is now loud rather than
    silently accepted.
    """
    bound: dict[str, str] = {}
    for node in module_body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    bound.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    bound.setdefault(
                        alias.asname or alias.name, f"{node.module}.{alias.name}"
                    )
    return bound


@functools.lru_cache(maxsize=None)
def _module_body_of(root: str, dotted: str) -> tuple[ast.stmt, ...] | None:
    """The top-level body of the repo module ``dotted`` names, if there is one.

    Resolved against ``root`` -- the working directory -- for the same reason
    ``_assert_symbol_exists`` opens ``tests/foo.py`` relative to it: a node id
    in this unit is a repo-relative path, and an import inside that file is a
    repo-rooted dotted path. Cached because the ancestry walk revisits the same
    handful of modules and because the cached body's identity is what the
    cycle guards below compare.
    """
    base = Path(root, *dotted.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            try:
                return tuple(ast.parse(candidate.read_text(encoding="utf-8")).body)
            except (OSError, SyntaxError):
                return None
    return None


def _dotted_target(base: ast.expr, module_body) -> str | None:
    """The dotted path a base EXPRESSION names, resolved through this module's imports."""
    parts: list[str] = []
    node = base
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = _dotted_bindings(module_body).get(node.id)
    if head is None:
        return None
    return ".".join([head, *reversed(parts)])


def _class_at(dotted: str | None, hops: int = 4) -> tuple[ast.ClassDef, tuple] | None:
    """The ``ClassDef`` at a dotted path, following re-exports a few hops."""
    if not dotted or "." not in dotted or hops <= 0:
        return None
    where, _, name = dotted.rpartition(".")
    body = _module_body_of(str(Path.cwd()), where)
    if body is None:
        return None
    found = next(
        (node for node in body if isinstance(node, ast.ClassDef) and node.name == name),
        None,
    )
    if found is not None:
        return found, body
    onward = _dotted_bindings(body).get(name)
    return _class_at(onward, hops - 1) if onward != dotted else None


def _base_class(base: ast.expr, module_body) -> tuple[ast.ClassDef, tuple] | None:
    """The class a base names -- defined here, or reached through an import.

    Round 22, and it is the boundary rounds 20 and 21 wrote down and mis-signed.
    Both of those docstrings said locally-defined bases only, "a false rejection
    is loud and a false acceptance is silent" -- which is true of
    ``_unittest_ancestry``, where an unresolved base means NOT collectible, and
    exactly backwards for the two predicates that inherited the sentence. An
    unresolved base carrying ``__test__ = False`` makes ``_resolved_test_flag``
    answer ``None``, which falls through to the name rule and ACCEPTS; an
    unresolved base carrying ``__init__`` makes ``_defines_constructor`` answer
    ``False``, which also accepts. pytest collects neither -- checked, not
    reasoned: the opt-out case is dropped without even a warning -- so the
    boundary was producing the silent direction it claimed to avoid.

    So the bases are followed across modules now, and anything still
    unresolvable is refused out loud by ``_collectible_class`` rather than
    guessed at. The justification did not travel with the code shape it was
    copied onto; that is the lesson, and it is round 21's own lesson about
    fixes inheriting blind spots, applied to a COMMENT.
    """
    local = {n.name: n for n in module_body if isinstance(n, ast.ClassDef)}
    if isinstance(base, ast.Name) and base.id in local:
        return local[base.id], tuple(module_body)
    return _class_at(_dotted_target(base, module_body))


def _unresolved_ancestry(node: ast.ClassDef, module_body, seen=frozenset()) -> list[str]:
    """Base spellings on this class's ancestry that resolve to nothing readable.

    A unittest base resolved by ``_unittest_ancestry`` is not one of them, and
    neither is ``object``: both are decided, just not by reading a file.
    """
    direct, packages = _unittest_names(module_body)
    stray: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Attribute):
            if base.attr in _UNITTEST_BASES and _root_name(base) in packages:
                continue
        elif isinstance(base, ast.Name):
            if base.id in direct or base.id == "object":
                continue
        resolved = _base_class(base, module_body)
        if resolved is None:
            stray.append(ast.unparse(base))
            continue
        parent, parent_body = resolved
        key = (id(parent_body), parent.name)
        if key in seen:
            continue
        stray.extend(_unresolved_ancestry(parent, parent_body, seen | {key}))
    return stray


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
    node: ast.ClassDef, module_body, seen: frozenset = frozenset()
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

    Round 22 lets the transitive half cross a module boundary, through the
    shared ``_base_class`` resolver: an intermediate base defined in a sibling
    helper file is the same intermediate the round-13 docstring promised to
    follow, and stopping at the file edge was an accident of where the
    dictionary of local classes came from.
    """
    direct, packages = _unittest_names(module_body)
    for base in node.bases:
        if isinstance(base, ast.Attribute):
            if base.attr in _UNITTEST_BASES and _root_name(base) in packages:
                return True
        elif isinstance(base, ast.Name) and base.id in direct:
            return True
        resolved = _base_class(base, module_body)
        if resolved is None:
            continue
        parent, parent_body = resolved
        key = (id(parent_body), parent.name)
        if key not in seen and _unittest_ancestry(parent, parent_body, seen | {key}):
            return True
    return False


def _test_flag(body: list[ast.stmt], attribute_of: str | None = None) -> bool | None:
    """The static ``__test__`` value stated in ``body``, or ``None`` if it says nothing.

    ``attribute_of`` looks for the OTHER spelling -- ``test_fn.__test__ = False``
    written in the scope that defines ``test_fn`` -- because pytest reads one
    attribute and does not care which statement set it.

    Only a literal can be mirrored statically. A computed ``__test__`` is not
    "no opinion": pytest evaluates it at runtime, so falling through to the
    name rule can silently accept a node pytest skips. Refuse that undecidable
    assignment out loud instead.
    """
    final_value: ast.expr | None = None
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
            if named:
                final_value = node.value
    if final_value is None:
        return None
    if not isinstance(final_value, ast.Constant):
        subject = attribute_of or "module/class"
        raise AssertionError(
            f"cannot decide pytest's computed __test__ value for {subject}"
        )
    return bool(final_value.value)


def _c3_merge(sequences: list[list[tuple[ast.ClassDef, tuple]]]):
    """Python's own linearization merge, over ``(class, body)`` pairs.

    Round 25. Written out rather than approximated because the approximation is
    the finding: a depth-first walk of the bases answers the FIRST flag it
    reaches, and C3 answers the first flag in linearized order, which is a
    different class the moment two bases share an ancestor.

    ``None`` means no consistent linearization exists -- the case where Python
    itself raises ``TypeError`` at class creation, so the module cannot import
    and pytest collects nothing from it at all.

    Classes are compared by the identity of their ``ClassDef`` node, never by
    ``(name, id(body))``: ``_base_class`` hands back a FRESH ``tuple(...)`` of
    the module body on every call, so a body-keyed comparison makes one class
    look like two and the merge silently degenerates into the depth-first order
    this function exists to replace. The ``ClassDef`` objects are stable because
    ``_module_body_of`` caches its parse.
    """
    pending = [list(seq) for seq in sequences if seq]
    order: list[tuple[ast.ClassDef, tuple]] = []
    while pending:
        for candidate in pending:
            head = candidate[0]
            if not any(id(cls) == id(head[0]) for seq in pending for cls, _ in seq[1:]):
                break
        else:
            return None
        order.append(head)
        for seq in pending:
            if id(seq[0][0]) == id(head[0]):
                del seq[0]
        pending = [seq for seq in pending if seq]
    return order


def _linearized_ancestry(
    node: ast.ClassDef, module_body, seen: frozenset = frozenset()
):
    """This class and its readable ancestors, in Python's attribute-lookup order.

    Bases that ``_base_class`` cannot read are skipped here and refused out
    loud by ``_collectible_class`` -- the round-22 division of labour, kept.
    ``None`` means the order is undecidable (a cycle, or an inconsistent
    hierarchy), which is not a shape that survives import.
    """
    key = id(node)
    if key in seen:
        return None
    parents = [
        resolved
        for resolved in (_base_class(base, module_body) for base in node.bases)
        if resolved is not None
    ]
    lines = []
    for parent, parent_body in parents:
        line = _linearized_ancestry(parent, parent_body, seen | {key})
        if line is None:
            return None
        lines.append(line)
    merged = _c3_merge(lines + [list(parents)])
    if merged is None:
        return None
    return [(node, module_body)] + merged


def _resolved_test_flag(node: ast.ClassDef, module_body) -> bool | None:
    """``__test__`` as pytest would READ it -- through the bases, not off the body.

    Round 20. ``_test_flag`` reads one class body, and pytest reads an
    ATTRIBUTE: ``getattr(obj, "__test__", True)`` walks the MRO, so
    ``class TestChild(Base)`` with ``__test__ = False`` on ``Base`` is not
    collected and neither of its methods runs. Reading only the child's own
    body called it collectible, and both readers of this predicate -- the corpus
    walk that discovers this unit's tests and the resolver that validates a
    cited node id -- would then advertise those methods as executable coverage.
    That is the same silent-citation rot the opt-out check was added to stop,
    one attribute lookup deeper than round 14 looked.

    Round 22 removes the local-only boundary this docstring used to defend with
    ``_unittest_ancestry``'s sentence -- "a false rejection is loud while a
    false acceptance is silent" -- which is true there and inverted here. An
    unreadable base carrying ``__test__ = False`` makes this function answer
    ``None``, the caller falls through to the name rule, and a ``Test*`` class
    pytest drops WITHOUT EVEN A WARNING is reported as collectible. So bases
    are followed across modules by ``_base_class``, and a base that is still
    unreadable is refused out loud by ``_collectible_class`` instead of being
    reasoned past here.

    Round 25 fixes the ORDER, which rounds 20 and 22 both got wrong in the same
    way: they walked the bases depth-first and returned the first flag found,
    while ``getattr`` reads the C3 linearization. The two disagree exactly when
    two bases share an ancestor -- ``Left(Common)`` and ``Right(Common)`` with
    the flag set on ``Common`` and overridden on ``Right`` -- because the walk
    reaches ``Common`` through ``Left`` and never examines ``Right`` at all.
    Both directions of that disagreement were checked against this repo's
    pytest, not reasoned: the depth-first answer accepts a class pytest drops
    in silence, and rejects one pytest collects.

    ``_defines_constructor`` deliberately keeps its depth-first walk. It mirrors
    ``hasinit``/``hasnew``, which ask whether ANY class in the MRO supplies the
    attribute -- an existential over the same set of ancestors, so order cannot
    change the answer and a linearization would buy nothing. The distinction is
    written down here because "fix the sibling too" is the tempting
    over-correction, and round 21 already recorded how a justification travels
    onto a shape it does not fit.
    """

    line = _linearized_ancestry(node, module_body)
    if line is None:
        return None
    for cls, _body in line:
        own = _test_flag(cls.body)
        if own is not None:
            return own
    return None


def _defines_constructor(
    node: ast.ClassDef, module_body, seen: frozenset = frozenset()
) -> bool:
    """``__init__``/``__new__`` as pytest would FIND them -- through the bases.

    Round 21, and it is round 20's finding one attribute over, in the function
    round 20 was editing. ``_resolved_test_flag`` was added because pytest
    reads ``__test__`` as an attribute, and the constructor refusal is the same
    lookup: pytest warns ``cannot collect test class 'TestChild' because it has
    a __init__ constructor`` for a class that merely INHERITS one, several hops
    up, and ``__new__`` behaves identically. Reading the child's own body
    called it collectible, so a catalog row could advertise executable
    coverage for methods pytest silently refuses to run.

    Round 22 removes the local-only boundary, and the sentence that used to
    justify it. This predicate errs the OPPOSITE way from ``_unittest_ancestry``
    whose reasoning it borrowed: an unreadable base with ``__init__`` on it
    makes this answer ``False``, which accepts a class pytest refuses by name
    -- the silent direction, not the loud one. Bases now resolve through
    ``_base_class`` across modules, and one that still will not resolve is
    refused out loud by ``_collectible_class``.
    """

    if any(
        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name in ("__init__", "__new__")
        for child in node.body
    ):
        return True
    for base in node.bases:
        resolved = _base_class(base, module_body)
        if resolved is None:
            continue
        parent, parent_body = resolved
        key = (id(parent_body), parent.name)
        if key not in seen and _defines_constructor(parent, parent_body, seen | {key}):
            return True
    return False


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

    Round 20 reads the flag through ``_resolved_test_flag`` rather than off the
    class body, because pytest reads an attribute and attributes are inherited.
    Round 21 does the same for the constructor rule, which round 20 left
    reading one class body while fixing the line above it -- the whole point
    being that pytest resolves BOTH through the MRO, so a fix that travels for
    one attribute and not the other is half a rule twice over.

    Round 22 makes the MRO travel across FILES, and adds the one answer this
    predicate never had: "I cannot tell". Rounds 20 and 21 followed only bases
    defined in the same module and justified the cut with ``_unittest_ancestry``
    's sentence about false rejections being loud -- which is backwards for
    both of them, because an unreadable base is exactly how ``__test__ = False``
    and an inherited ``__init__`` sneak past. So bases resolve through imports
    now, and a base that STILL resolves to nothing raises instead of being
    guessed at: undecidable is not the same as collectible, and the whole point
    of this predicate is that a citation may not advertise coverage pytest does
    not run.
    """
    flag = _resolved_test_flag(node, module_body)
    if flag is False:
        return False
    unittest_base = _unittest_ancestry(node, module_body)
    if not (unittest_base or node.name.startswith("Test") or flag):
        return False
    # Only asked on the ACCEPTING side. A class this rule would exclude anyway
    # needs no ancestry: excluding it is the loud direction -- a citation into
    # it fails right below with "pytest does not collect class" -- and asking
    # every helper class in the corpus to have a readable family tree would be
    # a rule about imports, not about collection.
    stray = _unresolved_ancestry(node, module_body)
    assert not stray, (
        f"cannot decide whether pytest collects class {node.name!r}: its "
        f"base(s) {stray} resolve to nothing readable from this repo, and both "
        f"``__test__`` and the constructor refusal are ATTRIBUTE lookups that "
        f"an unreadable ancestor can flip -- silently, in the opt-out case. "
        f"Give the base a repo-local definition this walker can reach, or stop "
        f"citing tests under this class; do not let it default to collectible"
    )
    if unittest_base:
        # The unittest plugin claims these, constructor and all: TestCase
        # itself defines ``__init__``, so the rule below cannot apply here.
        return True
    # Round 12, verified against this repo's pytest rather than reasoned: a
    # name-collected class that defines ``__init__`` or ``__new__`` is REFUSED
    # with ``PytestCollectionWarning: cannot collect test class ... because it
    # has a __init__ constructor``, and its tests never run. Believing the name
    # alone would let a catalog row cite a green-looking scenario that pytest
    # skips in silence -- the exact rot this resolver exists to stop, one
    # attribute deeper than round 11 looked. Round 21 resolves it through the
    # bases for the same reason round 20 resolved the flag through them.
    return not _defines_constructor(node, module_body)


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


def _validate_proof(proof: tuple[str, str]) -> None:
    kind, detail = proof
    assert kind in _PROOF_KINDS, proof
    assert detail.strip(), proof

    if kind == "covered":
        _assert_node_exists(detail)
    elif kind in {"shared", "defect"}:
        _assert_node_exists(_detail_node(detail))
    elif kind == "unproven":
        assert "probe" in detail.lower(), proof
    else:  # N/A
        assert len(detail.split()) >= 4, proof


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

    _validate_proof(proof)


def test_the_unproven_count_matches_the_checked_in_budget() -> None:
    """HFR-185: the size of the gap is a number, not an impression."""
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
        for finding_id in _FINDING_ID.findall(detail)
    }
    assert referenced == set(PR7R_FINDINGS), referenced
    for _b, _la, _t, _o, (kind, detail) in _CELLS:
        if kind == "defect":
            assert detail.split(" -- ")[0].strip() in PR7R_FINDINGS, detail


def test_a_citation_with_the_wrong_class_component_is_rejected() -> None:
    """HFR-187: the node resolver reads the whole id, not just its last word."""
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
    """HFR-189: the owner suffix is validated, not decoration."""
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
    """HFR-190: Q2's per-backend/per-lane obligation has no missing cells."""
    assert set(EXACT_TURN_PROGRESS_SIGNALS) == {
        (backend, lane) for backend in BACKENDS for lane in LANES
    }
    scenario_tests = {row["id"]: row["test"] for row in _scenario_rows()}
    for proof in EXACT_TURN_PROGRESS_SIGNALS.values():
        _validate_proof(proof)
        kind, detail = proof
        if kind == "defect":
            claim = detail.rpartition(" -- ")[0]
            scenario_ids = _SCENARIO_ID.findall(claim)
            assert len(scenario_ids) == 1, detail
            assert scenario_tests.get(scenario_ids[0]) == _detail_node(detail), detail
    kinds = {kind for kind, _reason in EXACT_TURN_PROGRESS_SIGNALS.values()}
    verdict = PR7R_QUESTIONS["Q2"]["verdict"]
    if kinds == {"unproven"}:
        assert verdict == "blocked", kinds
    elif kinds == {"covered"}:
        assert verdict == "answered", kinds
    else:
        assert verdict == "open", kinds


def test_every_pr7r_test_agrees_with_the_catalog_about_its_scenario_id() -> None:
    """HFR-192: the id in a docstring is the id in the catalog, and back again."""
    scenarios = _scenario_rows()
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
    """HFR-202: a scenario id is a stable name, so two rows may not answer to it."""
    scenarios = _scenario_rows()

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
    """HFR-193: the plan's verdict list and ``PR7R_QUESTIONS`` cannot disagree."""
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


_PLAN_BULLET = re.compile(r"^\d+\. \*\*(Q\d) [—-] ", re.MULTILINE)

#: Scope qualifiers an answer can carry. Each one is a phrase that RESTRICTS
#: what the verdict word claims, which is exactly the part HFR-193 leaves
#: unchecked: it ties the token an implementer greps for and says so.
#: A verdict word is equally true of "and here is what is settled about a Run"
#: and "and here is what is settled about a Turn", so the word alone cannot
#: keep the two documents from meaning different things.
_SCOPE_QUALIFIERS = ("run-scoped", "live dispatch", "claude only")


def _plan_verdict_bullets(text: str) -> dict[str, str]:
    """Each question's verdict bullet from the numbered block, flattened."""

    marks = list(_PLAN_BULLET.finditer(text))
    bullets: dict[str, str] = {}
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        bullets[match.group(1)] = _flatten(text[match.start() : end]).lower()
    return bullets


def test_the_plans_verdict_carries_the_same_scope_as_the_answer() -> None:
    """HFR-208: a scope the answer states, the plan's verdict must state too."""
    fixture = (
        "1. **Q1 — open.** it is run-scoped here\n"
        "2. **Q2 — open.** nothing qualified\n"
    )
    assert set(_plan_verdict_bullets(fixture)) == {"Q1", "Q2"}
    assert "run-scoped" in _plan_verdict_bullets(fixture)["Q1"]
    assert "run-scoped" not in _plan_verdict_bullets(fixture)["Q2"]

    start, end = _pr7r_plan_span(_PLAN)
    span = "\n".join(_PLAN.read_text(encoding="utf-8").splitlines()[start:end])
    bullets = _plan_verdict_bullets(span)
    assert set(bullets) == set(PR7R_QUESTIONS), sorted(bullets)

    checked = 0
    for key, question in PR7R_QUESTIONS.items():
        answer = _flatten(question["answer"]).lower()
        for qualifier in _SCOPE_QUALIFIERS:
            if qualifier not in answer:
                continue
            checked += 1
            assert qualifier in bullets[key], (
                f"{key}: the answer restricts itself to {qualifier!r} and the "
                f"plan's verdict bullet does not, so the plan claims the "
                f"broader thing in the document a follow-up unit treats as "
                f"the contract"
            )
    # Anti-degeneracy: a vocabulary no answer uses would make this pass on any
    # plan at all, which is the shape this unit keeps finding in its own guards.
    assert checked >= 2, "no scope qualifier was exercised; the guard is inert"


def test_the_plans_reserved_scenario_range_is_actually_free() -> None:
    """HFR-194: a reserved id block may not contain ids the catalog already owns."""
    scenarios = _scenario_rows()

    line = re.search(
        r"^- PR7: `HFR-(\d+)…(\d+)`.*?`HFR-(\d+)…(\d+)` still\s+reserved",
        _PLAN.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert line is not None, "the PR7 allocation line is not in the shape this guard reads"
    occupied = range(int(line.group(1)), int(line.group(2)) + 1)
    reserved = range(int(line.group(3)), int(line.group(4)) + 1)
    assert occupied.stop == reserved.start, (occupied, reserved)

    ours = _pr7r_owned_ids(scenarios)
    assert ours, "no catalog row points at a PR7R module"
    assert ours == set(occupied), (
        f"the plan claims HFR-{occupied.start}…{occupied.stop - 1} is occupied "
        f"by this unit. Outside that span: {sorted(ours - set(occupied))}. "
        f"Claimed but owned by no catalog row: {sorted(set(occupied) - ours)} "
        f"-- an id whose guard and row were deleted together leaves no other "
        f"trace, so narrow the claimed range or restore the evidence."
    )
    taken = {int(row["id"].removeprefix("HFR-")) for row in scenarios}
    assert not (taken & set(reserved)), sorted(taken & set(reserved))


#: A span starting at this unit's first id. Matched per LINE, not over the whole
#: document: the plan also recites the span historically ("the headline range
#: SAT at HFR-180…187") and hypothetically ("if HFR-180…219 has been taken by
#: then"), and neither is a claim about what is occupied now.
_OCCUPIED_RANGE = re.compile(r"`HFR-180…(\d+)`")

#: A line only CLAIMS the current occupation if it says so. Both live
#: statements and the allocation line carry this word; the historical and
#: hypothetical recitals do not.
_OCCUPANCY_CLAIM = re.compile(r"\bincrement\b", re.IGNORECASE)

#: The reserved tail, stated alongside the occupied range in the same two
#: places. Its low end is a derived fact too: one past the last occupied id.
_RESERVED_RANGE = re.compile(r"`HFR-(\d+)…219`.{0,20}?\breserved\b")

_PR7R_MODULES = frozenset(
    {
        "tests/test_run_terminal_truth_matrix.py",
        "tests/test_run_terminal_truth_evidence_probes.py",
    }
)


def _pr7r_owned_ids(scenarios: list[dict]) -> set[int]:
    """Every scenario number whose catalog row cites a PR7R module."""

    return {
        int(row["id"].removeprefix("HFR-"))
        for row in scenarios
        if row["test"].split("::")[0] in _PR7R_MODULES
    }


def test_every_claim_about_the_occupied_range_agrees_with_the_catalog() -> None:
    """HFR-209: the plan claims its own id range three times; all three must hold."""
    scenarios = _scenario_rows()
    ours = _pr7r_owned_ids(scenarios)
    assert ours, "no catalog row points at a PR7R module"
    highest = max(ours)

    claims = [
        (number, line)
        for number, line in enumerate(_PLAN.read_text(encoding="utf-8").splitlines(), 1)
        if _OCCUPANCY_CLAIM.search(line) and _OCCUPIED_RANGE.search(line)
    ]
    # Anti-degeneracy. A rewrap that puts the word and the span on different
    # lines would quietly stop checking one site, which is precisely the
    # failure mode this guard exists for, so the count is asserted too.
    assert len(claims) >= 3, (
        f"only {len(claims)} line(s) claim the occupied range; the banner, the "
        f"summary table and the allocation line all do, so either a claim was "
        f"deleted or a rewrap has moved one out of this guard's reach"
    )
    stale = [
        (number, int(value))
        for number, line in claims
        for value in _OCCUPIED_RANGE.findall(line)
        if int(value) != highest
    ]
    assert not stale, (
        f"the catalog gives PR7R ids up to HFR-{highest}, and these lines say "
        f"otherwise: {stale}. A reader picking the next free id from a stale "
        f"claim collides with evidence that already exists."
    )

    tails = [
        (number, int(value))
        for number, line in claims
        for value in _RESERVED_RANGE.findall(line)
    ]
    assert tails, "no line states the reserved tail beside the occupied range"
    detached = [pair for pair in tails if pair[1] != highest + 1]
    assert not detached, (
        f"reserved should start at HFR-{highest + 1}, one past the last "
        f"occupied id, and these lines disagree: {detached}"
    )


#: Modules this capability's catalog already cited without registering, before
#: PR7R added a rule about it. Written down rather than fixed: they belong to
#: units that closed long ago, and quietly appending twenty-two entries to a
#: shared reading list on an evidence-only branch would be a worse change than
#: the omission. The list is exact in both directions -- nothing new may join
#: it, and an entry that gets registered must be deleted from it -- so it can
#: only shrink. Same discipline as ``UNPROVEN_BUDGET``: a number that is wrong
#: on purpose, in writing, and cannot drift.
_INDEX_DEBT = frozenset(
    {
        "tests/test_agent_steering.py",
        "tests/test_agent_stop_settlement.py",
        "tests/test_claude_agent_initiated_turn.py",
        "tests/test_claude_agent_sessions.py",
        "tests/test_codex_agent.py",
        "tests/test_command_handler_user_names.py",
        "tests/test_controller_dispatch_loop.py",
        "tests/test_core_services_sessions.py",
        "tests/test_dispatcher_stream_chunk.py",
        "tests/test_harness_failure_visibility.py",
        "tests/test_harness_health_projection.py",
        "tests/test_inbox_bridge.py",
        "tests/test_inbox_events.py",
        "tests/test_message_dispatcher_result_fallback.py",
        "tests/test_message_dispatcher_scheduled.py",
        "tests/test_runtime_activation.py",
        "tests/test_runtime_ownership.py",
        "tests/test_runtime_recovery.py",
        "tests/test_runtime_work_supervisor.py",
        "tests/test_session_activities.py",
        "tests/test_session_fork.py",
        "tests/test_ui_server_fastapi.py",
    }
)


def test_the_capability_index_reaches_every_module_the_catalog_cites() -> None:
    """HFR-210: the canonical navigation path must reach the cited evidence."""
    yaml = pytest.importorskip("yaml")
    scenarios_dir = Path(__file__).resolve().parent / "scenarios"
    scenarios = _scenario_rows()

    index = yaml.safe_load((scenarios_dir / "INDEX.yaml").read_text(encoding="utf-8"))
    entries = index["capabilities"] if isinstance(index, dict) else index
    entry = next(
        item for item in entries if item.get("id") == "harness_failure_recovery"
    )
    listed = set(entry.get("scenario_tests") or []) | set(
        entry.get("unit_or_contract_tests") or []
    )
    cited = {row["test"].split("::")[0] for row in scenarios}
    missing = cited - listed
    assert not sorted(missing - _INDEX_DEBT), (
        f"{sorted(missing - _INDEX_DEBT)} are cited by harness_failure_recovery "
        f"catalog rows and are not reachable from the capability index, so the "
        f"repository's own zero-context navigation path omits them"
    )
    assert not sorted(_INDEX_DEBT - missing), (
        f"{sorted(_INDEX_DEBT - missing)} are registered in the index now, or "
        f"no longer cited; delete them from _INDEX_DEBT in the same commit so "
        f"the recorded gap stays the real one"
    )


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
    """HFR-196: a sentence counting Q2's covered cells must count the real table."""
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
    """HFR-198: the citation resolver's leaf rule, checked at both strictnesses."""
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
        "class TestFinalOptOut:\n"
        "    __test__ = True\n"
        "    __test__ = False\n"
        "    def test_case(self): ...\n"
        "class FinalOptIn:\n"
        "    __test__ = False\n"
        "    __test__ = True\n"
        "    def test_case(self): ...\n"
        "class MutedBase:\n"
        "    __test__ = False\n"
        "class TestInheritedOptOut(MutedBase):\n"
        "    def test_case(self): ...\n"
        "class MidBase(MutedBase):\n"
        "    pass\n"
        "class TestInheritedTwoDeep(MidBase):\n"
        "    def test_case(self): ...\n"
        "class OptedInBase:\n"
        "    __test__ = True\n"
        "class InheritedFlagIn(OptedInBase):\n"
        "    def test_case(self): ...\n"
        "class TestOverridesBase(MutedBase):\n"
        "    __test__ = True\n"
        "    def test_case(self): ...\n"
        "class MroLeft(OptedInBase):\n"
        "    pass\n"
        "class MroRight(OptedInBase):\n"
        "    __test__ = False\n"
        "class TestMroOptOut(MroLeft, MroRight):\n"
        "    def test_case(self): ...\n"
        "class MroLeftMuted(MutedBase):\n"
        "    pass\n"
        "class MroRightIn(MutedBase):\n"
        "    __test__ = True\n"
        "class TestMroOptIn(MroLeftMuted, MroRightIn):\n"
        "    def test_case(self): ...\n"
        "class CtorBase:\n"
        "    def __init__(self): ...\n"
        "class TestInheritedCtor(CtorBase):\n"
        "    def test_case(self): ...\n"
        "class CtorMid(CtorBase):\n"
        "    pass\n"
        "class TestInheritedCtorTwoDeep(CtorMid):\n"
        "    def test_case(self): ...\n"
        "class NewBase:\n"
        "    def __new__(cls): return super().__new__(cls)\n"
        "class TestInheritedNew(NewBase):\n"
        "    def test_case(self): ...\n"
        "class PlainBase:\n"
        "    def helper(self): ...\n"
        "class TestPlainBase(PlainBase):\n"
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
        "plain_named.__test__ = True\n"
        "def test_final_muted(): ...\n"
        "test_final_muted.__test__ = True\n"
        "test_final_muted.__test__ = False\n"
        "def plain_final_named(): ...\n"
        "plain_final_named.__test__ = False\n"
        "plain_final_named.__test__ = True\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    rel = Path("tests/sample_module.py")

    computed = tmp_path / "tests" / "computed_module.py"
    computed.write_text(
        "FLAG = False\n"
        "class TestComputedOptOut:\n"
        "    __test__ = FLAG\n"
        "    def test_case(self): ...\n"
        "def test_computed_muted(): ...\n"
        "test_computed_muted.__test__ = FLAG\n",
        encoding="utf-8",
    )
    computed_rel = Path("tests/computed_module.py")

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
    _assert_node_exists(f"{rel}::FinalOptIn::test_case")
    for opted_out in (
        "TestOptOut",
        "OptOutTests",
        "TestFlaggedCtor",
        "TestFinalOptOut",
    ):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{opted_out}'"
        ):
            _assert_node_exists(f"{rel}::{opted_out}::test_case")

    # Round 20: the same flag reached through a BASE. pytest reads an
    # attribute, and attributes are inherited, so ``TestInheritedOptOut`` is
    # collected by nobody while its name says otherwise -- checked against this
    # repo's pytest, which collects exactly the two classes asserted positive
    # below and neither of the two asserted negative. Four cases, because the
    # narrow fix is wrong three ways: the base may be one hop away or several,
    # an inherited opt-IN admits a class whose name says nothing, and a child's
    # own flag outranks whatever it inherited.
    for node_id in (
        f"{rel}::InheritedFlagIn::test_case",
        f"{rel}::TestOverridesBase::test_case",
    ):
        _assert_node_exists(node_id)
    for inherited_out in ("TestInheritedOptOut", "TestInheritedTwoDeep"):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{inherited_out}'"
        ):
            _assert_node_exists(f"{rel}::{inherited_out}::test_case")

    # Round 26: a computed flag is a third answer, not an absent flag. Pytest
    # evaluates it at runtime; accepting by name would silently advertise a
    # node that never runs. Both class and function spellings fail loudly.
    for undecidable in (
        f"{computed_rel}::TestComputedOptOut::test_case",
        f"{computed_rel}::test_computed_muted",
    ):
        with pytest.raises(
            AssertionError, match="cannot decide pytest's computed __test__ value"
        ):
            _assert_node_exists(undecidable)
    with pytest.raises(
        AssertionError, match="cannot decide pytest's computed __test__ value"
    ):
        _collected_tests(ast.parse(computed.read_text(encoding="utf-8")).body)

    # Round 25: the ORDER the inherited flag is read in, which round 20 left
    # depth-first and round 22 carried across module boundaries unexamined.
    # ``getattr`` reads the C3 linearization, and the walk reads the bases
    # left-first to the bottom, so the two disagree the moment two bases share
    # an ancestor -- and they disagree in BOTH directions, which is why both are
    # pinned. ``TestMroOptOut`` reaches ``__test__ = True`` through ``MroLeft``
    # while Python resolves ``MroRight``'s ``False`` first, and pytest collects
    # nothing from it -- silently, no warning. ``TestMroOptIn`` is the mirror:
    # the walk finds ``MutedBase``'s ``False`` through the left base and would
    # drop a class this repo's pytest does collect, which is the loud direction
    # but still wrong, and it is the case a one-sided fix would leave standing.
    _assert_node_exists(f"{rel}::TestMroOptIn::test_case")
    with pytest.raises(
        AssertionError, match="pytest does not collect class 'TestMroOptOut'"
    ):
        _assert_node_exists(f"{rel}::TestMroOptOut::test_case")

    # Round 21: the CONSTRUCTOR reached through a base, which is round 20's
    # finding one attribute over and was left standing by the commit that fixed
    # the flag. pytest refuses a ``Test*`` class that merely inherits
    # ``__init__`` or ``__new__`` -- the warning even names the constructor --
    # so these three are collected by nobody while their names say otherwise.
    # ``TestPlainBase`` is the control that keeps the rule from degenerating
    # into "any class with a local base is refused".
    _assert_node_exists(f"{rel}::TestPlainBase::test_case")
    for inherited_ctor in (
        "TestInheritedCtor",
        "TestInheritedCtorTwoDeep",
        "TestInheritedNew",
    ):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{inherited_ctor}'"
        ):
            _assert_node_exists(f"{rel}::{inherited_ctor}::test_case")

    # The same flag on a FUNCTION, which is written one scope out from the
    # function it applies to -- pytest reads an attribute and does not care
    # which statement set it, so the resolver reads the enclosing body.
    _assert_node_exists(f"{rel}::plain_named")
    _assert_node_exists(f"{rel}::plain_final_named")
    with pytest.raises(AssertionError, match="'test_muted' is opted out"):
        _assert_node_exists(f"{rel}::test_muted")
    with pytest.raises(AssertionError, match="'test_final_muted' is opted out"):
        _assert_node_exists(f"{rel}::test_final_muted")

    discovered = {suffix for suffix, _node in _collected_tests(ast.parse(
        module.read_text(encoding="utf-8")
    ).body)}
    assert discovered == {
        "test_real",
        "test_async_real",
        "plain_named",
        "plain_final_named",
        "TestGood::test_case",
        "OwnerTests::test_case",
        "CtorTests::test_case",
        "AliasedTests::test_case",
        "Derived::test_case",
        "FlaggedIn::test_case",
        "FinalOptIn::test_case",
        "InheritedFlagIn::test_case",
        "TestOverridesBase::test_case",
        "TestMroOptIn::test_case",
        "TestPlainBase::test_case",
    }, discovered
    for absent in (
        "Owner::test_case",
        "Helper::test_case",
        "TestOptOut::test_case",
        "OptOutTests::test_case",
        "TestInheritedOptOut::test_case",
        "TestInheritedTwoDeep::test_case",
        "TestMroOptOut::test_case",
        "TestFlaggedCtor::test_case",
        "TestFinalOptOut::test_case",
        "TestInheritedCtor::test_case",
        "TestInheritedCtorTwoDeep::test_case",
        "TestInheritedNew::test_case",
        "test_muted",
        "test_final_muted",
    ):
        assert absent not in discovered, absent

    # And the outermost level, which is round 10's lesson yet again: a module
    # setting ``__test__ = False`` is skipped WHOLE, so a bare ``def test_top``
    # in it never runs. Discovery returns nothing and every citation into it is
    # rejected -- checked on a separate module because the flag is file-scoped
    # and would have silenced the fixture above.
    muted = tmp_path / "tests" / "muted_module.py"
    muted.write_text(
        "__test__ = True\n"
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


def test_a_base_reached_through_an_import_is_resolved_or_refused_out_loud(
    tmp_path, monkeypatch
) -> None:
    """HFR-211: ancestry stops at no file boundary, and undecidable is not collectible."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "bases.py").write_text(
        "import unittest\n"
        "class MutedBase:\n"
        "    __test__ = False\n"
        "class CtorBase:\n"
        "    def __init__(self): ...\n"
        "class PlainBase:\n"
        "    def helper(self): ...\n"
        "class MidCase(unittest.TestCase):\n"
        "    pass\n",
        encoding="utf-8",
    )
    # One hop of re-export, which is how a package's ``__init__`` usually
    # publishes a helper: the name the importer sees is bound in a module that
    # does not define it.
    (tmp_path / "pkg" / "reexport.py").write_text(
        "from pkg.bases import MutedBase\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "imported_bases.py").write_text(
        "import pkg.bases\n"
        "from pkg.bases import CtorBase, MidCase, MutedBase, PlainBase\n"
        "from pkg.reexport import MutedBase as Muted2\n"
        "class TestImportedOptOut(MutedBase):\n"
        "    def test_case(self): ...\n"
        "class TestReexportedOptOut(Muted2):\n"
        "    def test_case(self): ...\n"
        "class TestImportedCtor(CtorBase):\n"
        "    def test_case(self): ...\n"
        "class TestDottedCtor(pkg.bases.CtorBase):\n"
        "    def test_case(self): ...\n"
        "class TestImportedPlain(PlainBase):\n"
        "    def test_case(self): ...\n"
        "class ImportedCaseTests(MidCase):\n"
        "    def test_case(self): ...\n",
        encoding="utf-8",
    )
    # The undecidable one, in its own file so the walk over the file above is
    # not cut short by the raise.
    (tmp_path / "tests" / "opaque_base.py").write_text(
        "from third_party.nowhere import Mystery\n"
        "class TestOpaque(Mystery):\n"
        "    def test_case(self): ...\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _module_body_of.cache_clear()
    rel = Path("tests/imported_bases.py")

    # Resolved across the file boundary, in both directions. ``PlainBase``
    # carries nothing, so the name rule stands; ``MidCase`` reaches
    # ``unittest.TestCase`` through an intermediate in another module, which is
    # the transitive case round 13 promised and round 22 finally delivers.
    _assert_node_exists(f"{rel}::TestImportedPlain::test_case")
    _assert_node_exists(f"{rel}::ImportedCaseTests::test_case")
    for refused in (
        "TestImportedOptOut",
        "TestReexportedOptOut",
        "TestImportedCtor",
        "TestDottedCtor",
    ):
        with pytest.raises(
            AssertionError, match=f"pytest does not collect class '{refused}'"
        ):
            _assert_node_exists(f"{rel}::{refused}::test_case")

    discovered = {
        suffix
        for suffix, _node in _collected_tests(
            ast.parse((tmp_path / rel).read_text(encoding="utf-8")).body
        )
    }
    assert discovered == {
        "TestImportedPlain::test_case",
        "ImportedCaseTests::test_case",
    }, discovered

    # And the third answer. Neither reader may guess: the citation check and
    # the discovery walk both go through the one predicate, so both raise, and
    # the message says what to do about it rather than what went wrong.
    opaque = Path("tests/opaque_base.py")
    with pytest.raises(AssertionError, match="cannot decide whether pytest collects"):
        _assert_node_exists(f"{opaque}::TestOpaque::test_case")
    with pytest.raises(AssertionError, match="cannot decide whether pytest collects"):
        _collected_tests(
            ast.parse((tmp_path / opaque).read_text(encoding="utf-8")).body
        )

    monkeypatch.undo()
    _module_body_of.cache_clear()


def test_a_mistyped_matrix_key_is_an_error_not_a_silent_fallback():
    """HFR-200: an override key the expansion cannot read must fail loudly."""
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


def test_a_mistyped_trigger_override_key_is_an_error_not_a_silent_fallback():
    """HFR-207: the same rule as HFR-200, at the level HFR-200 cannot see."""
    typo = {("direct_im", "scheduler_att"): {"success": {"shared": ("unproven", "x")}}}

    # The gap, demonstrated: expansion drops it, and HFR-200's guard is happy
    # with the result, because the result no longer contains the mistake.
    assert ("direct_im", "scheduler_att") not in RUN_TERMINAL_TRUTH_MATRIX
    _validate_matrix(
        {
            (lane, trigger): _lane_rows_probe()
            for lane in LANES
            for trigger in TRIGGERS
        }
    )

    with pytest.raises(ValueError, match=r"'scheduler_att' is not in TRIGGERS"):
        _validate_trigger_overrides(typo)

    with pytest.raises(ValueError, match=r"'durable_wokbench' is not in LANES"):
        _validate_trigger_overrides(
            {("durable_wokbench", "watch"): {"success": {"shared": ("unproven", "x")}}}
        )

    with pytest.raises(ValueError, match=r"outcome\(s\) \['sucess'\]"):
        _validate_trigger_overrides(
            {("direct_im", "watch"): {"sucess": {"shared": ("unproven", "x")}}}
        )

    with pytest.raises(ValueError, match="is not a \\(lane, trigger\\) pair"):
        _validate_trigger_overrides({"direct_im": {}})

    # And the checked-in overrides satisfy it. Non-empty, or this would pass on
    # a dict that had been emptied out -- the degenerate shape this unit keeps
    # finding, which a guard over a whitelist is especially prone to.
    assert _TRIGGER_OVERRIDES
    _validate_trigger_overrides(_TRIGGER_OVERRIDES)


def _lane_rows_probe() -> dict:
    """A minimal well-formed row set, so HFR-207 can feed HFR-200 real input."""

    return {outcome: {"shared": ("unproven", "probe")} for outcome in OUTCOMES}


_LEDGER_LITERALS = frozenset(
    f'"{phrase}",' for phrase, _round, _why in RETRACTED_PHRASINGS
)
#: The same rows as bare text, for the tokenized Python split, which sees a
#: string literal rather than the source line that carries it.
_LEDGER_PHRASES = frozenset(
    phrase.lower() for phrase, _round, _why in RETRACTED_PHRASINGS
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


#: The quote characters a Python string literal can open with, after any of the
#: ``r``/``b``/``f``/``u`` prefixes.
_STRING_OPENER = re.compile(r"^[A-Za-z]*(\"\"\"|'''|\"|')")


def _unquote(literal: str) -> str:
    """A Python string literal's text, without its prefix and delimiters.

    The delimiters have to go before the quote rule can read the literal: a
    docstring's own opening ``\"\"\"`` would otherwise pair with the first inner
    quote and put everything after it "outside" quotes.
    """
    opener = _STRING_OPENER.match(literal)
    if opener is None:  # pragma: no cover - tokenize guarantees a delimiter
        return literal
    return literal[opener.end() : -len(opener.group(1))]


def _py_prose_units(source: str) -> list[str]:
    """One unit per Python string literal and per contiguous comment block.

    Round 15, and the same defect round 14 fixed for YAML: a ``.py`` file
    flattened to one line, so the only thing separating a marker in one
    docstring from a banned phrasing three hundred lines below it was whether
    some sentence-ending period happened to fall between them. Where a docstring
    ends without one -- and most of the multi-line ones in this corpus end on a
    ``\"\"\"`` after a clause -- its tail merged with the head of the next
    literal into a single "sentence", and the scope rule ranged over both.

    Nothing drove this in round 14 because the YAML leak was the one a reviewer
    had found. It is driven now because the quote rule in ``_marker_near``
    cannot work without it: counting quote pairs across a whole flattened
    Python file pairs the close of one literal with the open of the next, and
    every answer is arbitrarily inside or outside quotes depending on how many
    unrelated strings precede it.

    Adjacent literals are merged, because implicit concatenation is one string
    to whoever reads it -- the same join ``_flatten`` did textually, done here
    where the token boundaries are actually known. A statement boundary
    (``NEWLINE``) or any other token ends the run; ``NL`` and indentation do
    not, since they are what a wrapped literal is made of.
    """
    units: list[str] = []
    buffer: list[str] = []
    kind: str | None = None

    def flush() -> None:
        nonlocal buffer, kind
        if buffer:
            joined = _flatten(" ".join(buffer))
            if joined and joined not in _LEDGER_PHRASES:
                units.append(joined)
        buffer, kind = [], None

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.STRING:
            if kind != "string":
                flush()
                kind = "string"
            buffer.append(_unquote(token.string))
        elif token.type == tokenize.COMMENT:
            if kind != "comment":
                flush()
                kind = "comment"
            buffer.append(re.sub(r"^#+\s?", "", token.string.strip()))
        elif token.type in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            continue
        else:
            flush()
    flush()
    return units


def _prose_units(path: Path, span: tuple[int, int] | None = None) -> list[str]:
    """The searchable prose of one artefact, split into independent scopes.

    A phrase in this corpus is routinely split across a line break, wrapped in a
    ``#`` comment, or spread over two adjacent Python string literals, so a
    naive substring search over the raw file finds none of them -- which would
    make the ledger below a guard that passes because it cannot see. Markdown is
    therefore still flattened whole: a wrapped paragraph IS one continuous
    statement there, and cutting it at line boundaries would hide every phrase
    that spans one.

    YAML is not, for the reason in ``_yaml_prose_units``; Python is not, for the
    reason in ``_py_prose_units``.

    The ledger's OWN row literals are dropped. Leaving them in would make every
    row trivially findable by its own definition, which is precisely how the
    "this row matches nothing" check would stop meaning anything.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py" and span is None:
        return _py_prose_units(text)
    lines = text.splitlines()
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


def _pr7r_prose_corpus() -> list[tuple[Path, tuple[int, int] | None]]:
    """Every artefact whose prose this unit polices, with the plan's PR7R span.

    Shared by HFR-201 and HFR-204 rather than spelled out twice: two guards
    sweeping "the corpus" from two hand-maintained lists is the drift this unit
    has already found four times in other shapes, and it would show up here as
    a rule that is corpus-wide in its docstring and file-wide in its code.
    """
    repo_root = Path(__file__).resolve().parents[1]
    plan = repo_root / "docs" / "plans" / "harness-run-reliability.md"
    scenarios = repo_root / "tests" / "scenarios" / "harness_failure_recovery"
    corpus: list[tuple[Path, tuple[int, int] | None]] = [
        (repo_root / "tests" / "run_terminal_truth_evidence.py", None),
        (repo_root / "tests" / "test_run_terminal_truth_matrix.py", None),
        (repo_root / "tests" / "test_run_terminal_truth_evidence_probes.py", None),
        (scenarios / "catalog.yaml", None),
        (scenarios / "observations.yaml", None),
        (plan, _pr7r_plan_span(plan)),
    ]
    for path, _span in corpus:
        assert path.exists(), path
    return corpus


def _inside_quotes(prose: str, start: int, end: int) -> bool:
    """Does a pair of double quotes enclose ``prose[start:end]``?

    Quotes are counted from the start of the prose unit, so an unbalanced quote
    can only ever make this stricter -- it shifts a later phrase from "inside"
    to "outside", which fails loudly, rather than the other way around.
    """
    for match in re.finditer(r'"([^"]*)"', prose):
        if match.start(1) <= start and end <= match.end(1):
            return True
    return False


def _marker_near(prose: str, start: int, end: int) -> bool:
    """Is a retraction marker in the phrase's OWN sentence, around a QUOTE of it?

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

    Round 15 adds the other half, found by counter-checking that round's own
    fix: the marker still only has to be SOMEWHERE in the sentence, and a
    sentence can carry a marker for a different retraction. Q3's answer opens
    "What IS established, and NARROWED in round 3 to what the probe actually
    reaches: ... one Turn-level ``source_kind`` stamped by the first
    participant" -- whose tail round 10 RETRACTED, rescued by one marker about
    round 3's narrowing sitting at the other end of it. Same accident as
    round 11's "narrower", one level up: the marker is real, it is just not
    about this phrase.

    So the phrase must also be QUOTED. That is not a new convention imposed on
    the corpus, it is what every real retraction here already does -- ``narrowed
    that from "..."``, ``said, and this test SUPERSEDED, "..."``, ``Q2's "..."
    was false`` -- and it is the difference between mentioning a claim and
    making one. An unquoted restatement is an assertion however many markers
    share its sentence.
    """
    if not _inside_quotes(prose, start, end):
        return False
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
    """HFR-201: a claim this unit retracted may only appear next to its retraction."""
    corpus = _pr7r_prose_corpus()

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
        '  round 9 wrote that "the two\n'
        f'  {low}"; round 10 retracted it.\n',
        encoding="utf-8",
    )
    assert _prose_units(folded) == [
        f'round 9 wrote that "the two {low}"; round 10 retracted it.'
    ]
    assert _unrescued(_prose_units(folded)) == []

    # Round 15, and it is round 11's accident one level up. A marker only has
    # to be somewhere in the sentence, and a sentence can carry a marker for a
    # DIFFERENT retraction: Q3's answer opens "and NARROWED in round 3 to what
    # the probe actually reaches" and went on, in the same sentence, to restate
    # round 10's banned claim as fact -- rescued by a marker that was about
    # something else entirely. Counter-checking round 15's own text edit is
    # what surfaced it: putting the claim back left this guard green.
    # So the phrase must be QUOTED as well, which is what every real retraction
    # in this corpus already does. Quoting a claim is mentioning it; saying it
    # unquoted is asserting it, however many markers share the sentence.
    assert not _accepts(
        f"what is established, and narrowed in round 3, is the two {banned}."
    )
    assert _accepts(
        f'what is established, and narrowed in round 3, is that "the two '
        f'{banned}" was the claim.'
    )

    # Round 15's second half is round 14's leak on the other half of the corpus.
    # A ``.py`` file was flattened whole, so a docstring ending without a full
    # stop -- which is most of the multi-line ones here -- merged with the
    # literal after it into one "sentence" and a marker in either vouched for
    # both. It is also what makes the quote rule above computable: counting
    # quote pairs across a whole file pairs the close of one literal with the
    # open of the next, and puts every phrase arbitrarily inside or outside.
    sample_py = tmp_path / "sample_prose.py"
    sample_py.write_text(
        "def a():\n"
        '    """round 10 retracted that claim"""\n'
        "def b():\n"
        f'    """the two {low} and nothing gates them"""\n'
        "FLAG = 1\n"
        f'# round 9 wrote "the two {low}"; it is false.\n',
        encoding="utf-8",
    )
    py_units = _prose_units(sample_py)
    assert "round 10 retracted that claim" in py_units, py_units
    assert _unrescued(py_units) == [f"the two {low} and nothing gates them"], py_units
    # ...and adjacent literals are still ONE unit, or every wrapped answer in
    # this corpus would be chopped at the quote that continues it -- the
    # over-correction that would hide any phrase spanning the join.
    wrapped_py = tmp_path / "wrapped_prose.py"
    wrapped_py.write_text(
        "ANSWER = (\n"
        f"    'round 9 wrote \"the two {low}\"'\n"
        "    'and round 10 retracted it.'\n"
        ")\n",
        encoding="utf-8",
    )
    assert _prose_units(wrapped_py) == [
        f'round 9 wrote "the two {low}" and round 10 retracted it.'
    ]
    assert _unrescued(_prose_units(wrapped_py)) == []

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


def test_a_scenario_id_named_in_an_answer_is_carried_as_that_answer_s_evidence() -> None:
    """HFR-203: leaning on a scenario in prose must mean citing its test."""
    scenarios = _scenario_rows()
    by_id = {row["id"]: row.get("test") for row in scenarios}

    def _unsupported(answer: str, evidence: tuple[str, ...]) -> list[str]:
        missing = []
        for scenario in sorted(set(re.findall(r"HFR-\d+", answer))):
            assert scenario in by_id, f"{scenario} names no catalog row"
            if by_id[scenario] not in evidence:
                missing.append(f"{scenario} ({by_id[scenario]})")
        return missing

    # Guard the guard, on the shape that actually occurred: an answer that
    # names a scenario whose test it does not carry, and the same answer once
    # the right test is cited. Built from real catalog rows so a renamed test
    # cannot leave this passing against a string that no longer exists.
    assert _unsupported("HFR-264 reconciles it", ()) == [
        f"HFR-264 ({by_id['HFR-264']})"
    ]
    assert _unsupported("HFR-264 reconciles it", (by_id["HFR-264"],)) == []
    # ...and naming the WRONG scenario is caught even when some evidence is
    # present, which is exactly how round 15's defect survived: Q5 had four
    # cited nodes and none of them was the row the sentence leaned on.
    assert _unsupported("HFR-261 reconciles it", (by_id["HFR-264"],)) == [
        f"HFR-261 ({by_id['HFR-261']})"
    ]

    offenders: list[str] = []
    for question, entry in PR7R_QUESTIONS.items():
        for missing in _unsupported(entry["answer"], tuple(entry["evidence"])):
            offenders.append(
                f"{question}'s answer leans on {missing}, which is not among "
                f"its evidence -- cite the test or stop leaning on the scenario"
            )
    assert not offenders, "\n".join(offenders)


#: A code span, in either of the two conventions this corpus uses: ``double``
#: in Python and YAML, `single` in the Markdown plan.
_CODE_SPAN = re.compile(r"`+([^`]+)`+")
#: ``HFR-nnn`` and a "drives" within a clause of it, in either order. The verb
#: is the one this unit actually writes when it attributes a behaviour to a
#: scenario; "holds"/"sends"/"stamps" were measured too and add nothing but
#: false positives, because those verbs take the SYMBOL as their subject.
_DRIVES_CLAIM = re.compile(
    r"\bhfr-\d{3}\b[^.]{0,40}?\b(?:drives|drove|driving)\b"
    r"|\b(?:drives|drove|driving)\b[^.]{0,40}?\bhfr-\d{3}\b"
)


def _production_symbols() -> set[str]:
    """Every ``def``/``class`` name defined outside the test tree, lowercased.

    Lowercased because ``_prose_units`` flattens to lower case, and matched on
    the leading dotted component of a span so ``CodexAgent.handle_message`` and
    ``_session_locks[base]`` both resolve to something checkable.
    """
    names: set[str] = set()
    repo_root = Path(__file__).resolve().parents[1]
    for package in ("core", "modules", "storage"):
        for module in (repo_root / package).rglob("*.py"):
            names.update(
                match.group(1).lower()
                for match in re.finditer(
                    r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)",
                    module.read_text(encoding="utf-8"),
                    re.M,
                )
            )
    return names


def _misattributed_guards(
    prose: str, guard_ids: set[str], production: set[str]
) -> list[str]:
    """Corpus-guard ids this prose unit credits with production behaviour.

    Sentence-scoped, and that scope is the whole design. Measured on the corpus
    at the time of writing: per prose UNIT the rule fires 14 times, of which 9
    are round-narration paragraphs that legitimately list which guards changed
    next to which production symbols were read -- a rule that noisy gets
    silenced. Per SENTENCE it fires 5 times and all 5 are round 16's defect.
    """
    found: list[str] = []
    bounds = [0, *(m.end() for m in _SENTENCE_BOUNDARY.finditer(prose)), len(prose)]
    for index in range(len(bounds) - 1):
        sentence = prose[bounds[index] : bounds[index + 1]]
        cited = sorted(set(re.findall(r"hfr-\d{3}", sentence)) & guard_ids)
        if not cited:
            continue
        spans = {
            re.split(r"[.\[(]", span)[0] for span in _CODE_SPAN.findall(sentence)
        } & production
        if spans or _DRIVES_CLAIM.search(sentence):
            found.extend(cited)
    return found


def test_a_corpus_guard_is_never_credited_with_production_behaviour() -> None:
    """HFR-204: a guard that reads this unit's text cannot drive the runtime."""
    scenarios = _scenario_rows()
    # Derived from the catalog's own ``test`` field rather than listed, so a
    # guard moved into the probes file stops being policed automatically and a
    # new guard added here starts being policed without an edit.
    guard_ids = {
        str(row["id"]).lower()
        for row in scenarios
        if Path(__file__).name in str(row.get("test") or "")
    }
    assert "hfr-193" in guard_ids, sorted(guard_ids)
    assert "hfr-195" not in guard_ids, "HFR-195 is a codex probe, not a corpus guard"

    production = _production_symbols()
    assert {"codexagent", "should_emit_progress"} <= production

    # Guard the guard, on the two shapes that occurred and the two that must
    # stay legal. Every fixture is CONCATENATED from a bare id and a bare
    # fragment rather than written out, because a literal spelling the whole
    # offending sentence would make this file an offender -- which is how the
    # first draft of this test failed, and the same trap round 15 hit when it
    # mechanised the retraction ledger.
    guard = "hfr-193"
    symbol = "``should_emit_progress``"
    assert _misattributed_guards(
        guard + " for the serialization.", guard_ids, production
    ) == []
    assert _misattributed_guards(
        symbol + " is correct filtering -- " + guard + " for the serialization.",
        guard_ids,
        production,
    ) == [guard]
    assert _misattributed_guards(guard + " drives that.", guard_ids, production) == [
        guard
    ]
    # Legal: the guard described as what it is, next to the test-side symbol it
    # reads. ``pr7r_questions`` is this unit's own table, not production.
    assert (
        _misattributed_guards(
            guard + " compares the plan's verdict against ``pr7r_questions``.",
            guard_ids,
            production,
        )
        == []
    )
    # Legal: a narration sentence that says which guards CHANGED. It names no
    # production symbol and claims no behaviour, and it is the shape that made
    # unit scope unusable.
    assert (
        _misattributed_guards(
            "the guards that changed are hfr-198, " + guard + " and hfr-201.",
            guard_ids,
            production,
        )
        == []
    )

    offenders: list[str] = []
    for path, span in _pr7r_prose_corpus():
        for prose in _prose_units(path, span):
            for scenario in _misattributed_guards(prose, guard_ids, production):
                offenders.append(
                    f"{path.name}: {scenario.upper()} is a corpus guard over "
                    f"PR7R's own text, but is cited here as driving production "
                    f"behaviour -- name the probe that drives it"
                )
    assert not offenders, "\n".join(sorted(set(offenders)))


def test_q4s_evidence_binds_a_run_and_never_a_turn() -> None:
    """HFR-206: a question about Turns may not rest on Run-scoped evidence."""
    activity_calls = 0
    for node_id in PR7R_QUESTIONS["Q4"]["evidence"]:
        _module_body, chain = _assert_symbol_exists(node_id)
        function = chain[-1]
        starts = [
            call
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "start"
            and any(keyword.arg == "activity_id" for keyword in call.keywords)
        ]
        assert starts, (
            f"{node_id}: cited as Q4 evidence but it registers no activity at "
            f"all -- the answer rests on the Activity output batch"
        )
        for call in starts:
            passed = {keyword.arg for keyword in call.keywords}
            activity_calls += 1
            assert "run_id" in passed, (node_id, sorted(passed))
            assert "turn_id" not in passed, (
                f"{node_id}: this activity binds a turn, so Q4's evidence is no "
                f"longer Run-scoped and the answer's scope disclaimer is stale"
            )
    assert activity_calls, "no activity registration inspected at all"

    # The prose half is the ledger's, and this asserts the ledger is actually
    # carrying it rather than assuming so: a row whose phrase drifted away from
    # the sentence it bans is the failure mode that ledger has already had.
    assert any(
        "pre-terminal evidence a turn carries" in phrase
        for phrase, _round, _why in RETRACTED_PHRASINGS
    ), "the conflating sentence is not enrolled; this guard is half a rule"
