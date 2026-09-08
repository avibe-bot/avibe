"""Cover the migration release guard's graph and attestation properties.

The synthetic tests pin the detection itself, so the guard cannot decay into something
that passes because it stopped looking. The repository tests run the guard for real and
exercise the release-history invariants it owns. Migration safety is intentionally an
explicit, finite attestation contract; these tests do not infer data guarantees from
Python or SQL source.

Anything reading release history needs tags, which a shallow CI checkout does not have.
Those tests skip there and the ``migration-release-guard`` workflow runs the same
properties through the CLI with the full history fetched.
"""

from __future__ import annotations

import inspect
import itertools
import json
import sqlite3
from pathlib import Path

import pytest
from alembic.script.revision import Revision

from scripts import migration_release_guard as guard
from scripts.release_package_version import package_version_from_release_tag
from storage.migrations import background_tables_ready

pytestmark = pytest.mark.no_sqlite_template


def _release_history() -> list[str]:
    try:
        return guard.released_tags()
    except guard.MigrationGuardError:
        return []


RELEASE_HISTORY = _release_history()

# The last release before the splice shipped. Comparing today's graph against it must
# still surface the rechain, which is what proves the guard detects the real defect and
# not merely a synthetic one.
SPLICED_BASELINE = "v3.0.10"
SPLICED_REVISION = "20260806_0047"

requires_release_history = pytest.mark.skipif(
    not RELEASE_HISTORY,
    reason=(
        "this checkout has no release tags carrying migrations; the migration-release-guard "
        "workflow runs these properties with fetch-depth: 0"
    ),
)
requires_spliced_baseline = pytest.mark.skipif(
    SPLICED_BASELINE not in RELEASE_HISTORY,
    reason=f"{SPLICED_BASELINE} is not present in this checkout",
)


def _graphs(monkeypatch, shipped: dict[str, str], current: dict[str, str]) -> None:
    monkeypatch.setattr(guard, "released_sources", lambda tag: shipped)
    monkeypatch.setattr(guard, "working_tree_sources", lambda: current)


def _revision_file(revision: str, down_revision: str, *, annotated: bool = False, **edges: str) -> str:
    """A migration module declaring exactly the graph fields it is given."""
    revision_type, parent_type = (": str", ": str | None") if annotated else ("", "")
    lines = [f'revision{revision_type} = "{revision}"', f"down_revision{parent_type} = {down_revision}"]
    lines += [f"{field} = {value}" for field, value in edges.items()]
    return '"""a migration"""\n\n' + "\n".join(lines) + "\n"


# One revision of every shape the real graph contains: a root with no parent, an ordinary
# linear child declared in the annotated form, a sibling branch carrying a branch label,
# and a merge with a tuple parent and a dependency edge. Seeding every shape rather than
# listing the ones that must not regress means a shape introduced later is covered the
# moment it joins this table, without editing a single test.
SHIPPED_REVISIONS = (
    ("20260101_0001", "root", "None", {}),
    ("20260102_0002", "linear", '"20260101_0001"', {"annotated": True}),
    ("20260103_0003", "branch", '"20260101_0001"', {"branch_labels": '("side",)'}),
    ("20260104_0004", "merge", '("20260102_0002", "20260103_0003")', {"depends_on": '"20260103_0003"'}),
)

SHIPPED_GRAPH = {
    f"{revision}_{slug}.py": _revision_file(revision, down_revision, **edges)
    for revision, slug, down_revision, edges in SHIPPED_REVISIONS
}


def test_an_unchanged_graph_reports_nothing(monkeypatch):
    _graphs(monkeypatch, SHIPPED_GRAPH, dict(SHIPPED_GRAPH))

    assert guard.rechained_revisions("v0.0.0") == []
    assert guard.new_slot_collisions("v0.0.0") == {}


@pytest.mark.parametrize(
    ("revision", "slug", "edges"),
    [(revision, slug, edges) for revision, slug, _, edges in SHIPPED_REVISIONS],
)
def test_repointing_any_released_revision_is_reported(monkeypatch, revision, slug, edges):
    current = dict(SHIPPED_GRAPH)
    current[f"{revision}_{slug}.py"] = _revision_file(revision, '"20269999_9999"', **edges)
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")
    assert len(problems) == 1
    assert revision in problems[0]


@pytest.mark.parametrize("field", guard.GRAPH_EDGES)
def test_changing_any_edge_alembic_orders_by_is_reported(monkeypatch, field):
    """Every field Alembic builds its graph from, not just the parent pointer.

    ``depends_on`` and ``branch_labels`` reorder a graph as surely as ``down_revision``
    does, and a dependency added behind a revision users are already stamped at is the
    outage's exact shape: fresh databases traverse it, existing ones record it as
    satisfied. Parametrizing off ``GRAPH_EDGES`` means a field added to the guard is
    covered here without editing this test.
    """
    declarations = {"down_revision": '"20260101_0001"'} | {field: '"20260103_0003"'}
    current = dict(SHIPPED_GRAPH)
    current["20260102_0002_linear.py"] = _revision_file("20260102_0002", annotated=True, **declarations)
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")

    assert len(problems) == 1
    assert field in problems[0]


def test_the_compared_fields_are_what_alembic_builds_its_graph_from():
    """The field list is measured against Alembic, never maintained by hand here.

    A field Alembic orders revisions by and this guard does not read is a hole of exactly
    the outage's shape -- the graph changes and every comparison still matches. Anchoring
    to the constructor means a field added upstream fails this test rather than going
    unwatched for however long it takes someone to notice.
    """
    parameters = set(inspect.signature(Revision.__init__).parameters) - {"self"}

    assert set(guard.GRAPH_FIELDS.values()) == parameters


@pytest.mark.parametrize(
    ("shipped", "respelled"),
    [('"20260101_0001"', '("20260101_0001",)'), ('("20260101_0001",)', '"20260101_0001"')],
    ids=["scalar-to-tuple", "tuple-to-scalar"],
)
def test_a_spelling_alembic_reads_identically_is_not_drift(monkeypatch, shipped, respelled):
    """Normalization is Alembic's, so the guard agrees with it about what changed.

    A guard that reported a re-spelling would be red for an edit that changes nothing,
    and a guard people expect to be wrong is one they stop reading.
    """
    _graphs(
        monkeypatch,
        dict(SHIPPED_GRAPH) | {"20260102_0002_linear.py": _revision_file("20260102_0002", shipped)},
        dict(SHIPPED_GRAPH) | {"20260102_0002_linear.py": _revision_file("20260102_0002", respelled)},
    )

    assert guard.rechained_revisions("v0.0.0") == []


def test_deleting_a_released_revision_is_reported(monkeypatch):
    current = {name: source for name, source in SHIPPED_GRAPH.items() if "linear" not in name}
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")
    assert len(problems) == 1
    assert "20260102_0002" in problems[0]


def test_a_slot_taken_twice_since_the_baseline_is_reported(monkeypatch):
    current = dict(SHIPPED_GRAPH)
    current["20260105_0004_second_claim.py"] = _revision_file("20260105_0004", '"20260104_0004"')
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    assert guard.new_slot_collisions("v0.0.0") == {
        "0004": {"20260104_0004_merge.py", "20260105_0004_second_claim.py"}
    }


def test_a_slot_the_baseline_already_shared_is_not_reported(monkeypatch):
    """Collisions history already carries stay out of the report, without an allowlist.

    The graph really does contain them. Reporting them would leave the guard permanently
    red -- and a permanently red guard is one people learn to ignore.
    """
    shipped = dict(SHIPPED_GRAPH)
    shipped["20260105_0004_second_claim.py"] = _revision_file("20260105_0004", '"20260104_0004"')
    _graphs(monkeypatch, shipped, dict(shipped))

    assert guard.new_slot_collisions("v0.0.0") == {}


def test_a_further_claimant_to_an_already_shared_slot_is_reported(monkeypatch):
    """What history excuses is the filenames it shipped, not the slot number forever.

    Excusing the number would make an already-duplicated slot a permanent blind spot --
    the one place a third branch could fork the graph without the guard saying anything.
    """
    shipped = dict(SHIPPED_GRAPH)
    shipped["20260105_0004_second_claim.py"] = _revision_file("20260105_0004", '"20260104_0004"')
    current = dict(shipped)
    current["20260106_0004_third_claim.py"] = _revision_file("20260106_0004", '"20260105_0004"')
    _graphs(monkeypatch, shipped, current)

    assert guard.new_slot_collisions("v0.0.0") == {
        "0004": {
            "20260104_0004_merge.py",
            "20260105_0004_second_claim.py",
            "20260106_0004_third_claim.py",
        }
    }


@pytest.mark.parametrize(
    "shipped_parent",
    ['"20260101_0001"', "ROOT"],
    ids=["shipped-a-literal", "shipped-a-computed-expression"],
)
def test_computed_revision_metadata_is_reported_rather_than_compared(monkeypatch, shipped_parent):
    """Metadata the guard cannot read is the absence of evidence, never evidence of equality.

    The second case is the one a normalized ``"<computed>"`` string got wrong: two
    different computed expressions rendered identically and so compared equal, which read
    as an unchanged graph precisely where nothing could be verified at all.
    """
    shipped = dict(SHIPPED_GRAPH)
    shipped["20260102_0002_linear.py"] = _revision_file("20260102_0002", shipped_parent)
    current = dict(shipped)
    current["20260102_0002_linear.py"] = _revision_file("20260102_0002", "SOME_OTHER_CONSTANT")
    _graphs(monkeypatch, shipped, current)

    problems = guard.rechained_revisions("v0.0.0")

    assert len(problems) == 1
    assert "20260102_0002_linear.py" in problems[0]
    assert "computes its migration metadata" in problems[0]


def test_two_files_claiming_one_revision_are_reported_rather_than_compared(monkeypatch):
    """Alembic identifies a migration by its revision string, and so must the guard.

    Two files declaring one identifier are indistinguishable to both: a database stamped
    with it has applied whichever one ran, and Alembic will never run the other's body.
    The dangerous outcome is not the collision but the silent survivor -- one claimant
    answering the comparison for a migration whose tables no database has.
    """
    current = dict(SHIPPED_GRAPH)
    current["20260105_0005_impostor.py"] = _revision_file("20260102_0002", '"20260101_0001"')
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")

    assert len(problems) == 2
    assert {"20260102_0002_linear.py", "20260105_0005_impostor.py"} == {problem.split()[0] for problem in problems}
    assert all("also declares" in problem for problem in problems)


# Every way a migration file's declared metadata can be malformed, alongside the well-formed
# shapes. Seeding the malformed ones rather than listing which failures must be caught is
# what makes the partition below complete: a new way to be unreadable joins this table and
# is covered without editing an assertion.
UNREADABLE_GRAPH = dict(SHIPPED_GRAPH) | {
    "20260105_0005_computed_revision.py": _revision_file("X", "None").replace('"X"', "SOME_CONSTANT"),
    "20260106_0006_computed_parent.py": _revision_file("20260106_0006", "SOME_CONSTANT"),
    "20260107_0007_computed_dependency.py": _revision_file(
        "20260107_0007", '"20260101_0001"', depends_on="SOME_CONSTANT"
    ),
    "20260108_0008_duplicate.py": _revision_file("20260102_0002", '"20260101_0001"'),
    "20260109_0009_no_metadata.py": '"""not a migration at all"""\n',
    "__init__.py": "",
}


@pytest.mark.parametrize("sources", [SHIPPED_GRAPH, UNREADABLE_GRAPH], ids=["well-formed", "malformed"])
def test_every_migration_is_compared_or_reported(sources):
    """The invariant behind every key this guard invents, stated once.

    A key names exactly one migration or it names none. Whatever a file declares, it is
    either a node the comparison reaches or a reason the comparison refuses to run --
    never neither, because a file that is neither has been dropped silently and the
    comparison then passes over a graph missing it.

    Coverage is the property; disjointness is not. Demanding the two halves never overlap
    is what made ``revision_graph`` drop what it could not read, which is right for the
    working tree -- the file is reported instead -- and silently wrong for a baseline,
    where dropping a node discards the only surviving record of what that release
    declared. An unreadable node stays in the graph carrying values that compare unequal
    to everything, so it is reached *and* reported.

    The denominator is the point. Every earlier version of this assertion counted the
    files the guard's own parser had managed to read, which cannot fail: a file it does
    not see is missing from both the numerator and the denominator at once. Counting the
    files Alembic will load is a measurement the parser cannot influence, so a migration
    that becomes invisible to it now fails here instead of passing quietly.
    """
    compared = {name for name, _ in guard.revision_graph(sources).values()}
    reported = set(guard.ungraphable_sources(sources))

    assert compared | reported == {name for name in sources if guard.is_migration_source(name)}


# Every shape a *released* migration's metadata can take: the readable ones, plus an edge
# no reader can resolve. A released revision the guard stops reading is not one it may
# quietly skip -- it is one whose declared parent nothing records any more, which is
# strictly worse than a parent that merely changed.
RELEASED_SHAPES = SHIPPED_REVISIONS + (("20260105_0005", "computed", "SOME_CONSTANT", {}),)

RELEASED_GRAPH = {
    f"{revision}_{slug}.py": _revision_file(revision, down_revision, **edges)
    for revision, slug, down_revision, edges in RELEASED_SHAPES
}


@pytest.mark.parametrize(("revision", "slug"), [(revision, slug) for revision, slug, _, _ in RELEASED_SHAPES])
def test_no_released_revision_can_be_rewritten_without_a_report(monkeypatch, revision, slug):
    """Whatever a release shipped, rewriting it in the working tree has to be visible.

    Stated per released shape rather than per known bug: the hole this closes was a
    released node with a computed edge, which the baseline graph dropped while the now
    readable working-tree node produced no reason of its own, so the rewrite was compared
    against nothing and reported by nobody. A shape whose baseline handling regresses
    fails here whatever the mechanism, and a shape added to the table is covered without
    editing an assertion.
    """
    name = f"{revision}_{slug}.py"
    current = dict(RELEASED_GRAPH)
    current[name] = _revision_file(revision, '"20269999_9999"')
    _graphs(monkeypatch, RELEASED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")

    assert [problem for problem in problems if revision in problem or name in problem]


@pytest.mark.parametrize("renamed", [False, True], ids=["in-place", "renamed"])
@pytest.mark.parametrize(("revision", "slug"), [(revision, slug) for revision, slug, _, _ in SHIPPED_REVISIONS])
def test_no_released_migration_body_can_change_without_a_report(monkeypatch, revision, slug, renamed):
    """What a released migration *does* is as fixed as what it declares.

    A database stamped at the revision never reruns the edited body and a fresh install
    runs only the new one, so the two diverge permanently with nothing raised at either
    moment. The edit here is a comment, which is the weakest case on purpose: an
    exemption for edits that look harmless would put a human judgement back on the path
    this guard exists to take it off.

    Renaming is the same edit wearing a filename, and both axes are stated together
    because they are one defect rather than a case and its afterthought. Alembic keys a
    migration by ``revision`` and scans the directory to find it, so the renamed file runs
    on every fresh install exactly as the original did -- while a check keyed by filename
    sees the old name absent and compares nothing.
    """
    name = f"{revision}_{slug}.py"
    current = dict(SHIPPED_GRAPH)
    edited = current.pop(name) + "\n# a later edit\n"
    current[f"{revision}_renamed.py" if renamed else name] = edited
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.edited_released_bodies("v0.0.0")

    assert len(problems) == 1
    assert revision in problems[0]


def test_renaming_a_released_migration_without_editing_it_is_not_a_change(monkeypatch):
    """The boundary the property above draws, stated from the side it must not cross.

    A database records the revision it reached and nothing else, and Alembic finds that
    revision by scanning ``version_locations`` rather than by name. A rename leaving the
    body alone is therefore invisible to every database in the field, and reporting it
    would be the guard enforcing a filename convention of its own -- which is the slot
    property's subject, not this one's.
    """
    revision, slug = SHIPPED_REVISIONS[0][0], SHIPPED_REVISIONS[0][1]
    current = dict(SHIPPED_GRAPH)
    current[f"{revision}_moved.py"] = current.pop(f"{revision}_{slug}.py")
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    assert guard.edited_released_bodies("v0.0.0") == []
    assert guard.rechained_revisions("v0.0.0") == []


# Every way a released revision can stop running what the release ran, keyed by the shape
# of the change rather than by which half of the pair happens to catch it.
RELEASED_REVISION_MUTATIONS = {
    "edited": lambda sources, name: dict(sources) | {name: sources[name] + "\n# a later edit\n"},
    "renamed-and-edited": lambda sources, name: {key: value for key, value in sources.items() if key != name}
    | {"20269999_9999_moved.py": sources[name] + "\n# a later edit\n"},
    "deleted": lambda sources, name: {key: value for key, value in sources.items() if key != name},
    "rechained": lambda sources, name: dict(sources) | {name: _revision_file("20260102_0002", '"20269999_9999"')},
    "made-unreadable": lambda sources, name: dict(sources)
    | {name: sources[name].replace('"20260102_0002"', "SOME_CONSTANT")},
    "duplicated": lambda sources, name: dict(sources) | {"20260102_0002_copy.py": sources[name]},
}


@pytest.mark.parametrize(
    "mutate", list(RELEASED_REVISION_MUTATIONS.values()), ids=list(RELEASED_REVISION_MUTATIONS)
)
def test_a_released_revision_is_always_accounted_for(monkeypatch, mutate):
    """The pair is exhaustive even where neither half is, and that is the claim on record.

    ``rechained_revisions`` watches what a revision declares and ``edited_released_bodies``
    watches what it does, so each passes over what the other owns -- a revision that is
    gone, contested, or unreadable has no body to compare, and a body edit under an
    unchanged declaration produces no drift. Asserting on their union is what makes those
    hand-offs safe: a case falling out of one half without landing in the other fails here
    rather than becoming a silently unguarded shape.
    """
    _graphs(monkeypatch, SHIPPED_GRAPH, mutate(SHIPPED_GRAPH, "20260102_0002_linear.py"))

    assert guard.rechained_revisions("v0.0.0") + guard.edited_released_bodies("v0.0.0")


def test_support_files_are_not_held_to_the_release(monkeypatch):
    """Alembic does not load ``__init__.py`` as a migration, so neither does the guard."""
    shipped = dict(SHIPPED_GRAPH) | {"__init__.py": ""}
    current = dict(shipped) | {"__init__.py": "# touched\n"}
    _graphs(monkeypatch, shipped, current)

    assert guard.edited_released_bodies("v0.0.0") == []
    assert guard.rechained_revisions("v0.0.0") == []


def _declare(monkeypatch, *entries: guard.DeclaredBodyEdit) -> None:
    monkeypatch.setattr(guard, "DECLARED_BODY_EDITS", entries)


DECLARED_EDIT = SHIPPED_GRAPH["20260102_0002_linear.py"] + "\n# a declared edit\n"


@pytest.mark.parametrize(
    ("pinned", "reported"),
    [
        (guard.body_fingerprint(DECLARED_EDIT), False),
        (guard.body_fingerprint(DECLARED_EDIT + "# and one more\n"), True),
    ],
    ids=["pins-the-body-present", "pins-a-different-body"],
)
def test_a_declaration_accepts_exactly_the_body_it_pins(monkeypatch, pinned, reported):
    """What separates a declaration from an exemption, stated from both sides.

    An entry naming only the revision would hand that file a standing permission: the
    edit it was written for and every later one look alike to a check that stops at the
    name. Pinning the replacement means the second edit arrives as an undeclared edit
    again, which is the whole difference between recording a decision and suspending the
    property.
    """
    current = dict(SHIPPED_GRAPH) | {"20260102_0002_linear.py": DECLARED_EDIT}
    _graphs(monkeypatch, SHIPPED_GRAPH, current)
    _declare(monkeypatch, guard.DeclaredBodyEdit(revision="20260102_0002", body=pinned, reason="recorded"))

    assert bool(guard.edited_released_bodies("v0.0.0")) is reported


def test_a_declaration_that_describes_no_edit_is_reported_as_spent(monkeypatch):
    """The property that keeps the list from becoming the allowlist this module refuses.

    A declaration is spent the moment a release ships the body it pinned, and a spent one
    is indistinguishable from a live one by inspection. Failing on it is what bounds the
    list to edits made since the last release, and it also catches the entry written for
    an edit that was reverted before shipping -- which would otherwise sit ready to
    excuse an edit nobody reviewed.
    """
    _graphs(monkeypatch, SHIPPED_GRAPH, dict(SHIPPED_GRAPH))
    _declare(monkeypatch, guard.DeclaredBodyEdit(revision="20260102_0002", body="0" * 64, reason="recorded"))

    assert len(guard.spent_body_edit_declarations("v0.0.0")) == 1
    assert guard.edited_released_bodies("v0.0.0") == []


def test_every_declaration_pins_a_fingerprint_and_records_a_reason():
    """A mistyped fingerprint would otherwise surface as an unexplained mismatch."""
    for declared in guard.DECLARED_BODY_EDITS:
        assert declared.revision.strip()
        assert declared.reason.strip()
        assert len(declared.body) == 64 and set(declared.body) <= set("0123456789abcdef")


@requires_release_history
def test_the_real_graph_has_no_undeclared_edit_and_no_spent_declaration():
    """The pair measured against the graph that actually ships, not a synthetic one.

    Both halves, because they fail in opposite directions: an undeclared edit is a
    divergence nobody wrote down, and a spent declaration is a written permission that
    outlived the edit it was written for.
    """
    assert guard.edited_released_bodies() == []
    assert guard.spent_body_edit_declarations() == []


def test_an_unreadable_graph_has_its_head_refused_rather_than_guessed():
    """A head is one answer, and a partial graph corrupts it silently rather than loudly.

    Dropping an unreadable node also drops the parent edge pointing past it, so its
    ancestor is left looking like a head. The upgrade property asserts a released database
    ends at a head, so an upgrade that stopped early at that ancestor would satisfy the
    very assertion the property exists to make.
    """
    with pytest.raises(guard.MigrationGuardError):
        guard.shipped_head_revisions(UNREADABLE_GRAPH)

    assert guard.shipped_head_revisions(SHIPPED_GRAPH) == {"20260104_0004"}


@requires_release_history
def test_the_real_graph_is_wholly_comparable():
    """The partition above, run against the graph that actually ships.

    Holding only a synthetic tree to it would leave the possibility that every real
    migration sits in the reported half, where nothing is ever compared.
    """
    assert guard.ungraphable_sources(guard.working_tree_sources()) == {}


@pytest.mark.parametrize(("problems", "expected_exit"), [([], 0), (["a released revision was rechained"], 1)])
def test_the_command_line_exit_code_follows_the_verdict(monkeypatch, capsys, problems, expected_exit):
    """The CLI is how a developer runs this outside CI, so its wiring is part of the guard."""
    monkeypatch.setattr(guard, "collect_problems", lambda baseline, **kwargs: ("v9.9.9", problems, {}))

    assert guard.main([]) == expected_exit
    for problem in problems:
        assert problem in capsys.readouterr().err


def test_the_command_line_refuses_rather_than_passing_without_history(monkeypatch, capsys):
    def unreachable(*args, **kwargs):
        raise guard.MigrationGuardError("no release tag carries the migrations directory")

    monkeypatch.setattr(guard, "collect_problems", unreachable)

    assert guard.main([]) == 2
    assert "could not run" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('MIGRATION_SAFETY = "copy"\n', "copy"),
        ('MIGRATION_SAFETY: str = "additive"\n', "additive"),
        ('MIGRATION_SAFETY = ("copy",)\n', guard.COMPUTED),
        ('MIGRATION_SAFETY = ["copy"]\n', guard.COMPUTED),
        ('MIGRATION_SAFETY = make_safety()\n', guard.COMPUTED),
        ('MIGRATION_SAFETY = "copy"\nMIGRATION_SAFETY = "backfill"\n', guard.COMPUTED),
        ('if enabled:\n    MIGRATION_SAFETY = "copy"\n', None),
        ("", None),
    ],
    ids=["string", "annotated-string", "tuple", "list", "computed", "duplicate", "nested", "absent"],
)
def test_migration_safety_declaration_is_one_top_level_literal(source, expected):
    """The declaration reader never executes migration code or evaluates SQL."""
    actual = guard.declared_migration_safety(source)
    if expected is guard.COMPUTED:
        assert actual is guard.COMPUTED
    else:
        assert actual == expected


@pytest.mark.parametrize("kind", sorted(guard.MIGRATION_SAFETY_KINDS))
def test_each_safety_kind_is_an_accepted_attestation(kind):
    assert guard._migration_safety_declaration_error("r", kind) is None


@pytest.mark.parametrize(
    ("declaration", "expected_fragment"),
    [
        (None, "no MIGRATION_SAFETY"),
        (guard.COMPUTED, "computed or malformed"),
        ("future", "unsupported kind"),
        ("copy", None),
    ],
    ids=["missing", "computed", "unsupported", "accepted"],
)
def test_safety_attestation_shape_fails_closed(declaration, expected_fragment):
    error = guard._migration_safety_declaration_error("r", declaration)
    if expected_fragment is None:
        assert error is None
    else:
        assert expected_fragment in error


def test_attestation_is_metadata_only_and_does_not_execute_alembic(monkeypatch):
    """The graph supplies every subject without a second migration executor."""
    revision = "20260105_0005"
    shipped = dict(SHIPPED_GRAPH)
    current = dict(shipped)
    current[f"{revision}_new.py"] = _revision_file(revision, '"20260104_0004"') + 'MIGRATION_SAFETY = "additive"\n'
    _graphs(monkeypatch, shipped, current)
    monkeypatch.setattr(guard, "latest_released_tag", lambda: "v0.0.0")
    monkeypatch.setattr(guard, "released_graphs", lambda: pytest.fail("attestation must not enumerate upgrade graphs"))
    monkeypatch.setattr(guard.command, "upgrade", lambda *args, **kwargs: pytest.fail("attestation must not execute Alembic"))

    assert guard.migration_safety_problems() == []


def test_attestation_boundary_uses_latest_release_not_comparison_baseline(monkeypatch):
    """An older metadata comparison cannot re-enroll revisions already released later."""
    released_revision = "20260105_0005"
    latest = dict(SHIPPED_GRAPH)
    latest[f"{released_revision}_released.py"] = _revision_file(released_revision, '"20260104_0004"')
    current = dict(latest)
    monkeypatch.setattr(guard, "latest_released_tag", lambda: "v0.0.1")
    monkeypatch.setattr(guard, "released_sources", lambda tag: SHIPPED_GRAPH if tag == "v0.0.0" else latest)
    monkeypatch.setattr(guard, "working_tree_sources", lambda: current)
    monkeypatch.setattr(guard, "releases_with_state_but_no_graph", lambda: [])
    monkeypatch.setattr(guard, "fresh_install_tables", lambda: guard.HEAD_TABLES)
    monkeypatch.setattr(guard, "new_slot_collisions", lambda baseline: {})
    monkeypatch.setattr(guard, "rechained_revisions", lambda baseline: [])
    monkeypatch.setattr(guard, "edited_released_bodies", lambda baseline: [])
    monkeypatch.setattr(guard, "spent_body_edit_declarations", lambda baseline: [])
    monkeypatch.setattr(guard, "unrepairable_releases", lambda: pytest.fail("--skip-upgrade must skip runtime upgrades"))

    _, problems, refused = guard.collect_problems("v0.0.0", include_upgrade=False)

    assert problems == []
    assert refused == {}


def test_only_revisions_after_latest_release_require_attestation(monkeypatch):
    """A missing declaration is reported for a new revision, never for grandfathered history."""
    released_revision = "20260105_0005"
    new_revision = "20260106_0006"
    latest = dict(SHIPPED_GRAPH)
    latest[f"{released_revision}_released.py"] = _revision_file(released_revision, '"20260104_0004"')
    current = dict(latest)
    current[f"{new_revision}_new.py"] = _revision_file(new_revision, f'"{released_revision}"')
    monkeypatch.setattr(guard, "latest_released_tag", lambda: "v0.0.1")
    monkeypatch.setattr(guard, "released_sources", lambda tag: latest)
    monkeypatch.setattr(guard, "working_tree_sources", lambda: current)

    problems = guard.migration_safety_problems()

    assert problems == [f"{new_revision} has no MIGRATION_SAFETY declaration"]


@pytest.mark.parametrize(
    ("declaration", "expected_fragment"),
    [
        ("", "no MIGRATION_SAFETY"),
        ('MIGRATION_SAFETY = "additive"\n', None),
        ('MIGRATION_SAFETY = "future"\n', "unsupported kind"),
        ('MIGRATION_SAFETY = ("copy",)\n', "computed or malformed"),
    ],
    ids=["missing", "accepted", "unknown", "non-string"],
)
def test_new_revision_requires_exact_attestation(monkeypatch, declaration, expected_fragment):
    revision = "20260105_0005"
    shipped = dict(SHIPPED_GRAPH)
    current = dict(shipped)
    current[f"{revision}_new.py"] = _revision_file(revision, '"20260104_0004"') + declaration
    _graphs(monkeypatch, shipped, current)
    monkeypatch.setattr(guard, "latest_released_tag", lambda: "v0.0.0")

    problems = guard.migration_safety_problems()
    if expected_fragment is None:
        assert problems == []
    else:
        assert any(expected_fragment in problem and revision in problem for problem in problems)


@requires_release_history
def test_real_release_baseline_has_no_new_migration_safety_obligations():
    """A current graph with no post-release revisions needs no new attestation run."""
    assert guard.migration_safety_problems() == []


def test_head_tables_is_what_a_fresh_install_has():
    """The derivation source for the upgrade property must itself be measured, not maintained.

    Every other check compares against ``HEAD_TABLES``. If a migration adds a table and
    nobody adds it here, the upgrade property stops asking about it and goes on passing.
    """
    assert guard.fresh_install_tables() == guard.HEAD_TABLES


def test_a_complete_set_of_tables_is_not_by_itself_a_complete_schema(tmp_path):
    """Readiness has to mean what production means by it, columns included.

    An upgrade that creates every table and omits a column leaves a database that starts
    and then fails on the first query touching it -- indistinguishable from success to
    anything comparing table names alone.
    """
    db_path = tmp_path / "vibe.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        for table in guard.HEAD_TABLES:
            connection.execute(f'create table "{table}" (placeholder integer)')
        connection.commit()
    finally:
        connection.close()

    assert not background_tables_ready(db_path)
    gap = guard.describe_schema_gap(db_path)
    assert "table(s) missing" not in gap
    assert "column(s) missing" in gap


def test_a_prerelease_sorts_between_the_releases_it_falls_between():
    """Installable prereleases are releases here, ordered where they actually shipped.

    ``gh-vX.Y.ZrcN`` builds carry a wheel and an sdist, so a database in the field can
    have been built by one. Sorting them as releases is what makes the newest tag a
    baseline rather than a guess.
    """
    assert (
        guard.version_key("v3.0.8")
        < guard.version_key("gh-v3.0.9rc2")
        < guard.version_key("gh-v3.0.9rc10")
        < guard.version_key("v3.0.9")
    )
    # The publisher writes the same version several ways and builds the same wheel from
    # each, so the guard has to read them as the same release rather than as one release
    # and some strings it does not recognise.
    assert guard.release_version("gh-v3.0.9-rc2") == guard.release_version("gh-v3.0.9rc2")


@requires_release_history
def test_no_tag_the_publisher_can_build_falls_outside_the_guard():
    """The release universe is the publish path's, because that is the one that decides.

    A tag the guard does not recognise is absent from every property here, so a migration
    first shipped in it can be rechained or edited afterwards with nothing to compare
    against -- the guard would pass, quietly, over the release it was pointed at.

    The denominator is the repository's own tags run through the publish path's parser,
    not a list of the forms in use today. Every tag shipped so far happens to be plain
    ``vX.Y.Z`` or ``gh-vX.Y.ZrcN``, so a filter narrowed back to those would satisfy any
    test written from today's tags and drop the first release that used another form.
    """
    covered = set(guard.released_tags())
    for tag in guard._git("tag", "-l", "v*", "-l", "gh-v*").split():
        try:
            package_version_from_release_tag(tag)
        except ValueError:
            continue
        assert tag in covered or guard.versions_tree(tag) is None, tag


@pytest.mark.parametrize(
    ("gap", "short", "expected"),
    [
        (None, {}, []),
        (None, {"messages": "0 of 2 rows; CHECK constraint failed: ck_messages_type"}, ["could not seed messages"]),
        ("1 table(s) missing: scopes", {}, ["upgrade left the database not ready"]),
    ],
    ids=["reaches-head-over-rows", "a-table-it-could-not-seed", "a-schema-that-came-out-short"],
)
def test_a_release_the_property_could_not_cover_is_reported_as_a_failure(monkeypatch, gap, short, expected):
    """Coverage the run did not reach is a violation, not a note printed beside them.

    A note blocks nothing, so a guard that downgrades its own gaps into notes goes on
    reporting success over the part of its subject it never touched. That is the same
    failure as passing over a release that fell outside the window, and it is worse for
    being visible: the run says both "passed" and "did not look", and only one is read.
    """
    monkeypatch.setattr(guard, "released_graphs", lambda: ["v9.9.9"])
    monkeypatch.setattr(guard, "schema_gap_after_upgrade", lambda tag: (gap, short, {}))

    reasons = guard.unrepairable_releases()[0].get("v9.9.9", [])

    assert len(reasons) == len(expected)
    assert all(fragment in reason for reason, fragment in zip(reasons, expected))


@requires_release_history
def test_every_release_that_wrote_a_database_is_inside_the_upgrade_window():
    """The upgrade property's window is an equation about releases, so it is checked as one.

    ``released_tags`` reads "shipped no versions directory" as "left no migrated database
    in the field". That is true today because state and the graph arrived together --
    everything before ``storage/`` persisted JSON, which is why ``storage/importer.py``
    still carries an importer for it rather than a migration -- but it is an equation, and
    a release breaking it would leave a database no property here builds while every
    property here goes on passing. Falling out of a window is the failure with no symptom.

    The second assertion is what keeps the first from being a tautology: the denominator is
    every tag the publisher can build, which is strictly larger than the tags carrying a
    graph. Read off the graph-carrying set instead, the property could never be violated
    and the test would pass forever.
    """
    universe = guard.release_tag_names()
    graphed = guard.released_tags()

    assert guard.releases_with_state_but_no_graph() == []
    assert set(graphed) < set(universe)


def test_every_table_the_schema_accepts_rows_for_gets_them(tmp_path):
    """An upgrade proved on an empty database is proved against the one case no user is in.

    Empty is where adding a NOT NULL column, tightening a nullable one, and building a
    unique index all succeed unconditionally, so it is the state under which a migration
    that cannot survive real data still passes.

    The claim is a pair, the way the released-revision checks are: a table either carries
    its rows or is named in what the seeder returns, with the schema's own objection.
    Asserting only that some tables were seeded would pass a seeder that gave up on the
    awkward ones quietly, and quietly giving up on the awkward ones is the failure that
    reads most like success.
    """
    db_path = tmp_path / "vibe.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("create table plain (id text primary key, name text not null)")
        connection.execute(
            "create table enumerated (id text primary key, state text not null "
            "constraint ck_enumerated_state check (state in ('waiting', 'active')))"
        )
        connection.execute("create table defaulted (id integer primary key, note text default 'n')")
        connection.execute(
            "create table shaped (id text primary key, doc text not null "
            "constraint ck_shaped_doc check (json_valid(doc) = 1 "
            "and json_extract(doc, '$.version') = 1 "
            "and json_type(doc, '$.events') = 'array'))"
        )
        connection.execute("create table constrained (id text primary key, slug text not null)")
        connection.execute("create unique index ux_constrained_slug on constrained (slug)")
        connection.execute(
            "create table paired (platform text not null, native_id text not null, "
            "label text not null, primary key (platform, native_id))"
        )
        connection.execute(
            "create table pinned (id text primary key, kind text not null "
            "constraint ck_pinned_kind check (kind in ('only')), ref text not null)"
        )
        connection.execute("create unique index ux_pinned_kind_ref on pinned (kind, ref)")
        connection.execute(
            "create table overlapping (a text not null, b text not null, c text not null, note text)"
        )
        connection.execute("create unique index ux_overlapping_ab on overlapping (a, b)")
        connection.execute("create unique index ux_overlapping_bc on overlapping (b, c)")
        connection.execute("create table nullable (id text primary key, tag text, note text)")
        connection.execute(
            "create table exclusive (id text primary key, kind text not null, left_ref text, right_ref text, "
            "constraint ck_exclusive_shape check ("
            "(kind = 'left' and left_ref is not null and right_ref is null) or "
            "(kind = 'right' and right_ref is not null and left_ref is null) or "
            "(kind = 'none' and left_ref is null and right_ref is null)))"
        )
        connection.execute(
            "create table impossible (id text primary key, shape text not null "
            "constraint ck_impossible_shape check (shape in ('a', 'b') and shape in ('c', 'd')))"
        )
        # A parent whose key a CHECK holds to one literal, so a child cannot guess the value it
        # ends up with, and two children of it: one whose reference may repeat, and one whose
        # reference is its own primary key and therefore may not.
        connection.execute(
            "create table parented (id text primary key "
            "constraint ck_parented_id check (id in ('kept')), note text)"
        )
        connection.execute(
            "create table sharing (id text primary key, parent_id text not null references parented (id), "
            "note text)"
        )
        connection.execute(
            "create table exclusively_owned (parent_id text primary key references parented (id), "
            "note text)"
        )
        connection.commit()
    finally:
        connection.close()

    short, refused = guard.seed_representative_rows(db_path)

    connection = sqlite3.connect(db_path)
    try:
        counted = {
            str(name): connection.execute(f'select count(*) from "{name}"').fetchone()[0]
            for (name,) in connection.execute("select name from sqlite_master where type = 'table'")
        }
        state = connection.execute("select state from enumerated").fetchone()[0]
        doc = connection.execute("select doc from shaped").fetchone()[0]
        slugs = [row[0] for row in connection.execute("select slug from constrained")]
        names = [row[0] for row in connection.execute("select name from plain")]
        pairs = [tuple(row) for row in connection.execute("select platform, native_id from paired")]
        pins = [tuple(row) for row in connection.execute("select kind, ref from pinned")]
        overlaps = [tuple(row) for row in connection.execute("select a, b, c from overlapping")]
        tags = [row[0] for row in connection.execute("select tag from nullable")]
        kinds = [tuple(row) for row in connection.execute("select kind, left_ref, right_ref from exclusive")]
        shared = [row[0] for row in connection.execute("select parent_id from sharing")]
        owned = [row[0] for row in connection.execute("select parent_id from exclusively_owned")]
        dangling = connection.execute("pragma foreign_key_check").fetchall()
    finally:
        connection.close()

    assert set(short) == {"impossible", "parented", "exclusively_owned"}
    assert "ck_impossible_shape" in short["impossible"]
    assert all(count >= guard.SEED_ROWS for table, count in counted.items() if table not in short)
    # Every repair came from the constraint that rejected the row before it, not from a guess.
    assert state in {"waiting", "active"}
    assert json.loads(doc) == {"version": 1, "events": []}
    # A column repeats unless the schema refuses the row that repeats it, and which of the two
    # it is stays the database's answer rather than this module's model of the schema. `slug`
    # and `plain.id` are refused, so those tables fall back to rows differing everywhere;
    # `plain.name` is free, and a migration adding `unique (name)` meets the duplicate.
    assert len(set(slugs)) == len(slugs) == guard.SEED_ROWS
    assert len(set(names)) == 1
    # A composite group constrains the tuple, so each of its members still repeats -- which
    # takes more rows than the group is wide.
    assert len(set(pairs)) == len(pairs)
    assert len({platform for platform, _ in pairs}) < len(pairs)
    assert len({native_id for _, native_id in pairs}) < len(pairs)
    # Overlapping groups share a member, and the shared one is what a per-group choice gets
    # wrong: varying one member of each group leaves `b` distinct in every row, so a later
    # `unique (b)` migration passes here and fails on a release free to repeat it.
    assert len({(a, b) for a, b, _ in overlaps}) == len(overlaps)
    assert len({(b, c) for _, b, c in overlaps}) == len(overlaps)
    assert all(len({row[member] for row in overlaps}) < len(overlaps) for member in range(3))
    # A CHECK pinning one member of a group to a single literal is the schema declining the
    # other's repeat: every row that holds `ref` still carries `kind = 'only'` and collides.
    assert len(set(pins)) == len(pins)
    assert len({kind for kind, _ in pins}) == 1
    # A nullable column carries both shapes a release holds. Only the non-NULL one is new: a
    # migration tightening the column or reading its value is untested against NULLs alone,
    # and one made non-NULL everywhere is untested against the NULLs it will actually meet.
    assert None in tags
    assert [tag for tag in tags if tag is not None]
    # Some tables have no row with every column non-NULL, and no fixture can be asked for one:
    # `ck_exclusive_shape` keys `left_ref` and `right_ref` off `kind`, so each state requires
    # one of them NULL. The table still gets rows, in whichever state the repair reached, and
    # the columns that leaves NULL are what `refused` exists to say out loud rather than to
    # pass over. Reaching every state instead is constraint solving, which this is not.
    assert "exclusive, every column at once" in refused
    assert "ck_exclusive_shape" in refused["exclusive, every column at once"]
    assert "exclusive" not in short
    assert {(left, right) for _, left, right in kinds} == {(None, None)}
    # Every reference resolves, which is not a nicety: `20260806_0047` rebuilds tables and then
    # runs this same check, aborting the upgrade if anything dangles, so a fabricated reference
    # turns a correct migration red. It cannot be resolved by guessing either -- `ck_parented_id`
    # holds the parent's key to one literal, so only reading back what the parent ended up with
    # gets it right.
    assert dangling == []
    assert set(shared) == {"kept"}
    # Whether the children may share that parent is the database's answer: `sharing` keeps its
    # reference in every row, while `exclusively_owned` has it as a primary key and so is bounded
    # by the one parent row there is. Sharing regardless would collapse it to a single row, and
    # one row is what this whole fixture exists to stop being.
    assert owned == ["kept"]
    assert f"of {guard.SEED_ROWS} rows" in short["exclusively_owned"]
    # And the bound is reported at both ends rather than only where it bites. `parented` may hold
    # one row because its key is held to one literal; `exclusively_owned` may hold one because
    # each of its rows needs a parent of its own. Naming only the child would read as the child's
    # problem, and naming neither is the shortfall-as-a-note this whole split exists to refuse.
    assert f"of {guard.SEED_ROWS} rows" in short["parented"]


def test_a_repeat_the_rows_do_not_carry_is_a_violation_however_it_got_there():
    """The claim is read back out of the rows, never inferred from the code that wrote them.

    ``insert_seed_row``'s repair rewrites values to satisfy whatever constraint a row tripped,
    and it picks the column to rewrite out of that constraint's own text -- so it can pick the
    one column the row existed to hold still. SQLite then accepts a row that proves nothing,
    and an accepted insert looks exactly like a successful one until the column is read back.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("create table t (a text not null, b text not null)")
        connection.executemany("insert into t (a, b) values (?, ?)", [("x", "x"), ("x-1", "x")])

        assert guard.seeded_rows_prove_repetition(connection, "t", ["b"]) == ""
        assert "a" in guard.seeded_rows_prove_repetition(connection, "t", ["a", "b"])
    finally:
        connection.close()


def test_current_schema_gets_valid_rows_in_every_table(tmp_path):
    """New constrained tables must be seedable even before their first release tag exists."""
    db_path = tmp_path / "vibe.sqlite"
    guard.run_migrations(db_path)

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        tables = {
            name
            for (name,) in connection.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            )
            if name != guard.ALEMBIC_BOOKKEEPING_TABLE
        }
        assert tables == guard.HEAD_TABLES
        for table in tables:
            assert connection.execute(f'select count(*) from "{table}"').fetchone()[0] >= guard.SEED_ROWS, table
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]
        assert connection.execute("pragma foreign_key_check").fetchall() == []


@pytest.mark.parametrize("minimum", [0, 4])
@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
@pytest.mark.parametrize("mixed_case", [False, True])
@pytest.mark.parametrize("source_check", ["a glob 'R-[A-C][0-9]'", "a in ('R-A0')"])
def test_shape_repairs_follow_constraints_not_column_names(tmp_path, minimum, order, mixed_case, source_check):
    db_path = tmp_path / "vibe.sqlite"
    columns = "id integer primary key, a text not null, b text not null, c text not null, d integer not null, e integer not null"
    if mixed_case:
        columns = columns.upper()
    checks = (
        f"constraint ck_a check ({source_check})",
        "constraint ck_b check (length(b) = 12 and b not glob '*[^0-9a-f]*')",
        "constraint ck_c check (length(c) = 9 and substr(c, 1, 4) = a)",
        f"constraint ck_counts check (typeof(d) = 'integer' and d >= 0 "
        f"and typeof(e) = 'integer' and e >= 0 and d + e > {minimum} and (d = 0 or c = ''))",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped ({columns}, {', '.join(checks[index] for index in order)})")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("columns,expression,witness", [
    ("code TEXT NOT NULL,state TEXT NOT NULL", "code GLOB 'ready' AND state=lower(code)", ('ready', 'ready')),
    ("a TEXT NOT NULL,b TEXT NOT NULL", "a GLOB 'A' AND b GLOB 'B' AND (a=b OR 1)", ('A', 'B')),
    ("state TEXT NOT NULL,value BLOB NOT NULL", "typeof(value)='blob' AND length(value)=0 AND state='ready'", ('ready', b'')),
    ("n0 INTEGER NOT NULL,n1 INTEGER NOT NULL,n2 INTEGER NOT NULL", "max(n1,n2)=2 AND (n0>0)+(n1>0)+(n2>0)=1", (0, 2, 0)),
])
def test_candidate_boundaries_preserve_native_seedable_rows(tmp_path, columns, expression, witness):
    db_path = tmp_path / 'candidate-boundaries.sqlite'
    ddl = f'create table shaped({columns},constraint ck check({expression}))'
    with sqlite3.connect(':memory:') as connection:
        connection.execute(ddl)
        connection.execute('insert into shaped values(' + ','.join('?' for _ in witness) + ')', witness)
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f'select count(*) from shaped where {expression}').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("dependency", ["state=lower(code)", "lower(code)=state", 'state="lower"(code)', 'state=trim(code)'])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("pattern", ['ready', 'r[ea]ady', "it''s ready"])
def test_computed_glob_values_remain_candidates_for_functional_dependents(tmp_path, dependency, reverse, separate, pattern):
    clauses = [f"code glob '{pattern}'", dependency]
    if reverse:
        clauses.reverse()
    checks = ','.join(f'constraint ck{i} check({clause})' for i, clause in enumerate(clauses)) if separate else f"constraint ck check({' and '.join(clauses)})"
    db_path = tmp_path / 'functional-glob.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f'create table shaped(state text not null,code text not null,{checks})')
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped where state=code').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("condition", [
    '(a=b OR 1)', 'NOT(a=b)', 'NOT NOT(a=b OR 1)',
    'CASE WHEN a=b THEN 0 ELSE 1 END', 'iif(a=b,0,1)',
    '(a=b)=0', '(a=b) IS FALSE', '(a=b) BETWEEN 0 AND 1',
])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
def test_conditional_equalities_do_not_restrict_independent_shapes(tmp_path, condition, reverse, separate):
    clauses = ["a glob 'A'", "b glob 'B'", condition]
    if reverse:
        clauses.reverse()
    checks = ','.join(f'constraint ck{i} check({clause})' for i, clause in enumerate(clauses)) if separate else f"constraint ck check({' and '.join(clauses)})"
    db_path = tmp_path / 'conditional-equality.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f'create table shaped(a text not null,b text not null,{checks})')
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where a='A' and b='B'").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("expression,joined", [
    ('a=b OR a=c AND b=c', False),
    ('NOT (a=b AND b=c)', False),
    ('CASE WHEN 1 THEN a=b AND b=c ELSE 1 END', False),
    ('(a=b) AND (((b=c)))', True),
    ('/* note */ ((a=b AND b=c)) -- end\n', True),
    ('1 BETWEEN 0 AND 2 AND a=b AND b=c', True),
    ('a=b AND b=c AND (a=c OR 1)', True),
])
def test_equality_groups_require_unconditional_whole_terms(expression, joined):
    assert guard.column_equality_groups(expression, ['a', 'b', 'c']) == ([['a', 'b', 'c']] if joined else [['a'], ['b'], ['c']])


@pytest.mark.parametrize("value,declared", [(b'', 'BLOB'), (b'ab', 'BLOB'), (b'\x00a', 'BLOB'), (17, 'INTEGER'), (1.5, 'REAL')])
@pytest.mark.parametrize("with_glob", [False, True])
def test_already_valid_native_shapes_preserve_storage(value, declared, with_glob):
    with sqlite3.connect(':memory:') as connection:
        width = connection.execute('select length(?)', (value,)).fetchone()[0]
        expression = f'length(value)={width} and state=\'ready\''
        if with_glob:
            expression += " and value not glob 'X*'"
        proposals = guard.shape_proposals(connection, expression, [('state', 'TEXT'), ('value', declared)], {'state': 'x', 'value': value})
        assert not any(name == 'value' for name, _ in proposals.derived)


@pytest.mark.parametrize("declared,storage", [('BLOB', 'blob'), ('INTEGER', 'integer'), ('REAL', 'real')])
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("state_position", [0, 1, -1])
def test_native_glob_groups_survive_unrelated_repairs(tmp_path, declared, storage, count, reverse, separate, state_position):
    names = [f'v{i}' for i in range(count)]
    clauses = [*(f"typeof({name})='{storage}'" for name in names),
               *(f'{left}={right}' for left, right in zip(names, names[1:])), "v0 not glob 'x'", "state='ready'"]
    if reverse:
        names.reverse()
        clauses.reverse()
    checks = ','.join(f'constraint ck{i} check({clause})' for i, clause in enumerate(clauses)) if separate else 'constraint ck check(' + ' and '.join(clauses) + ')'
    columns = [name+' '+declared+' not null' for name in names]
    columns.insert(len(columns) if state_position == -1 else state_position, 'state text not null')
    db_path = tmp_path / 'native-group.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(columns)},{checks})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("value,declared", [(b'', 'BLOB'), (b'abc', 'BLOB'), (b'\x00a', 'BLOB'), (17, 'INTEGER'), (1.5, 'REAL')])
@pytest.mark.parametrize("equality", ['=', '==', 'IS'])
def test_group_storage_preservation_uses_native_shape_semantics(value, declared, equality):
    with sqlite3.connect(':memory:') as connection:
        width = connection.execute('select length(?)', (value,)).fetchone()[0]
        expression = f"a {equality} b and length(a)={width} and b not glob 'X*' and state='ready'"
        proposals = guard.shape_proposals(connection, expression, [('a', declared), ('b', declared), ('state', 'TEXT')], {'a': value, 'b': value, 'state': 'x'})
        assert not any(name in {'a', 'b'} for name, _ in proposals.derived)


@pytest.mark.parametrize("collation,left,right", [('NOCASE', 'A', 'a'), ('RTRIM', 'A', 'A ')])
@pytest.mark.parametrize("equality", ['=', '==', 'IS'])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
def test_collated_groups_keep_independent_glob_witnesses(tmp_path, collation, left, right, equality, reverse, separate):
    clauses = [f"a glob '{left}'", f"b glob '{right}'", f'length(a)={len(left)}', f'length(b)={len(right)}', f'a {equality} b']
    if reverse:
        clauses.reverse()
    checks = ','.join(f'constraint ck{i} check({clause})' for i, clause in enumerate(clauses)) if separate else 'constraint ck check(' + ' and '.join(clauses) + ')'
    db_path = tmp_path / 'collated-group.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f'create table shaped(a text collate {collation} not null,b text not null,{checks})')
        connection.execute('insert into shaped values (?,?)', (left, right))
        connection.execute('delete from shaped')
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert set(connection.execute('select a,b from shaped')) == {(left, right)}
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("operator", ['=', '==', 'IS'])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("name", ['state', 'quoted state', 'a"b'])
def test_explicit_literal_bindings_precede_broadcasts(operator, reverse, name):
    identifier = guard.quote_identifier(name)
    literal = "'it''s ready'"
    binding = f'{literal} {operator} {identifier}' if reverse else f'{identifier} {operator} {literal}'
    expression = f"typeof(a)='blob' and (({binding}))"
    assert guard.check_proposals(expression, ['a', name])[0] == (name, "it's ready")


def test_independent_glob_witnesses_do_not_bypass_binary_equality(tmp_path):
    db_path = tmp_path / 'binary-group.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table shaped(a text not null,b text not null,constraint ck check(a glob 'A' and b glob 'a' and a=b))")
    short, _ = guard.seed_representative_rows(db_path)
    assert 'ck' in short['shaped']
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] == 0


@pytest.mark.parametrize("count", [31, 58, 59, 60, 80])
@pytest.mark.parametrize("context,reverse", itertools.product([False, True], repeat=2))
def test_large_numeric_domains_reach_joint_candidates(tmp_path, count, context, reverse):
    names = [f'n{i}' for i in range(count)]
    if reverse:
        names.reverse()
    expression = '+'.join(f'abs({name}-1)' for name in names) + '=0'
    if context:
        expression += ' and changes()>=0'
    db_path = tmp_path / 'large-joint.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("changed", range(39))
@pytest.mark.parametrize("reverse", [False, True])
def test_sparse_wave_past_half_budget_reaches_every_column(tmp_path, changed, reverse):
    names = [f'n{i}' for i in range(39)]
    clauses = ['+'.join(f'({name}>0)' for name in names) + '=1', f'abs(n{changed})>0']
    if reverse:
        names.reverse()
        clauses.reverse()
    db_path = tmp_path / 'wide-sparse.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({' and '.join(clauses)}))")
        witness = [int(name == f'n{changed}') for name in names]
        connection.execute('insert into shaped values (' + ','.join('?' for _ in names) + ')', witness)
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]
        connection.execute('delete from shaped')
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("count", range(31, 57))
@pytest.mark.parametrize("ranked_first", [False, True])
def test_first_sparse_wave_and_joint_anchors_share_fitting_budget(count, ranked_first):
    candidates = [0, 1, -1, 2]
    domain = [1, 0, -1, 2] if ranked_first else candidates
    current = (0,) * count
    expected = {tuple(value for _ in range(count)) for value in candidates}
    expected.update((*current[:index], 1, *current[index+1:]) for index in range(count))
    assert len(expected) <= guard.SEED_ATTEMPTS
    prefix = list(itertools.islice(guard.numeric_assignments([domain] * count, current, candidates), guard.SEED_ATTEMPTS))
    assert len(prefix) == len(set(prefix)) == guard.SEED_ATTEMPTS
    assert expected <= set(prefix)


@pytest.mark.parametrize("count", [31, 38, 40, 50, 55, 56])
@pytest.mark.parametrize("reverse", [False, True])
def test_wide_sparse_boundary_consumers_keep_last_column(tmp_path, count, reverse):
    names = [f'n{i}' for i in range(count)]
    clauses = ['+'.join(f'({name}>0)' for name in names) + '=1', f'abs(n{count-1})>0']
    if reverse:
        names.reverse()
        clauses.reverse()
    db_path = tmp_path / 'sparse-boundary.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute('select count(*) from shaped').fetchone()[0]
        if count <= 40:
            assert short == {}
            assert rows >= guard.SEED_ROWS
        else:
            # First-wave coverage is not a promise to exhaust later anchor
            # neighborhoods when constructing repeated rows. A shortfall must
            # still fail visibly rather than accepting a one-row fixture.
            assert rows >= 1
            assert bool(short) == (rows < guard.SEED_ROWS)
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("count", [31, 39, 59, 60, 80])
@pytest.mark.parametrize("target", [-1, 1, 2])
@pytest.mark.parametrize("context", [False, True])
def test_wide_joint_anchors_precede_remaining_sparse_wave(tmp_path, count, target, context):
    names = [f'n{i}' for i in range(count)]
    expression = '+'.join(f'abs({name}-({target}))' for name in names) + '=0'
    if context:
        expression += ' and changes()>=0'
    db_path = tmp_path / 'wide-joint-anchor.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert set(connection.execute('select * from shaped')) == {(target,) * count}
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("count", [31, 59, 80])
@pytest.mark.parametrize("held", [0, 1, -1])
def test_wide_fallback_anchor_survives_rebased_sparse_rows(count, held):
    names = [f'n{i}' for i in range(count)]
    expression = '+'.join(f'abs({name}-1)' for name in names) + '=0 and changes()>=0'
    with sqlite3.connect(':memory:') as connection:
        ddl = f"create table shaped({','.join(name+' integer' for name in names)},constraint ck check({expression}))"
        connection.execute(ddl)
        values = dict.fromkeys(names, 2)
        values[names[held]] = 1
        attempts = []
        connection.set_trace_callback(lambda sql: attempts.append(sql) if sql.startswith('insert into "shaped"') else None)
        assert guard.insert_seed_row(connection, 'shaped', ddl, [(name, 'INTEGER') for name in names], values) == ('', True)
        assert len(attempts) == 3
        assert connection.execute('select * from shaped').fetchall() == [(1,) * count]


@pytest.mark.parametrize("count", [31, 59, 80])
@pytest.mark.parametrize("context", [False, True])
def test_wide_ranked_assignment_survives_many_uniform_anchors(tmp_path, count, context):
    names = [f'n{i}' for i in range(count)]
    expression = ' and '.join(f'{name}={i+1}' for i, name in enumerate(names))
    if context:
        expression += ' and changes()>=0'
    db_path = tmp_path / 'wide-ranked.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert set(connection.execute('select * from shaped')) == {tuple(range(1, count+1))}
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("function", ['max(n1,n2)', 'MAX(n1,n2,0)', '"max"(n1,n2)', 'max /* call */ (n1,n2)', 'max(min(n1,2),n2)'])
@pytest.mark.parametrize("reverse", [False, True])
def test_scalar_overloads_keep_native_joint_evaluation(tmp_path, function, reverse):
    clauses = [f'{function}=2', '(n0>0)+(n1>0)+(n2>0)=1']
    if reverse:
        clauses.reverse()
    db_path = tmp_path / 'scalar-overload.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(n0 int not null,n1 int not null,n2 int not null,constraint ck check({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("default", ['max(1,2)', 'min(2,3,4)', 'min(max(1,2),3)'])
def test_scalar_overloads_in_defaults_preserve_native_candidates(default):
    with sqlite3.connect(':memory:') as connection:
        proposals = guard.shape_proposals(connection, 'n=2 and tag=2', [('n', 'INTEGER')], {'n': 0}, omitted=[('tag', 'INTEGER', default)])
        assert proposals.derived == [('n', 2)]
        assert proposals.numeric_fallback == []


@pytest.mark.parametrize("function", ['max(n)', 'min(n)', 'max(n,random())', 'min(n,changes())'])
def test_aggregate_and_contextual_overloads_still_defer(function):
    with sqlite3.connect(':memory:') as connection:
        proposals = guard.shape_proposals(connection, f'n>0 and {function}=2', [('n', 'INTEGER')], {'n': 0})
        assert proposals.derived == []
        assert proposals.numeric_fallback


def test_numeric_candidates_use_the_column_affinity():
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(connection, "n > '0'", [("n", "INTEGER")], {"n": 0}).derived
        assert ("n", 1) in proposals
        assert ("n", -1) not in proposals


@pytest.mark.parametrize("declared", ["INTEGER", "REAL"])
@pytest.mark.parametrize("shape", ["length(n)=2", "n glob '[1-9][0-9]'", "substr(n,1,2)='10'"])
@pytest.mark.parametrize("reverse", [False, True])
def test_strict_numeric_shape_repairs_remain_storable(tmp_path, declared, shape, reverse):
    if declared == "REAL":
        shape = shape.replace("length(n)=2", "length(n)=4").replace("'[1-9][0-9]'", "'[1-9][0-9].0'")
    clauses = [shape, "n=10"]
    if reverse:
        clauses.reverse()
    db_path = tmp_path / "strict-shape.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(n {declared} not null,constraint ck check({' and '.join(clauses)})) STRICT")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where n=10").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("changed", range(17))
def test_initial_sparse_round_reaches_every_column_before_joint_search(changed):
    required = [(f"n{i}", "INTEGER") for i in range(17)]
    expression = '+'.join(f'(n{i}>0)' for i in range(17)) + f'=1 and abs(n{changed})>0'
    with sqlite3.connect(":memory:") as connection:
        assert guard.shape_proposals(connection, expression, required, dict.fromkeys((n for n, _ in required), 0)).derived == [(f"n{changed}", 1)]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("literal", ["'ready'", "'it''s ready'", "17", "1.5"])
@pytest.mark.parametrize("declared", ["", "TEXT", "NUMERIC"])
def test_json_extraction_dependencies_keep_member_values(tmp_path, reverse, equality, literal, declared):
    dependency = f"state {equality} json_extract(doc,'$.x')"
    if reverse:
        dependency = f"json_extract(doc,'$.x') {equality} state"
    db_path = tmp_path / "json-dependency.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(doc text not null,state {declared} not null,constraint ck check(json_valid(doc) and json_extract(doc,'$.x')={literal} and {dependency}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where state=json_extract(doc,'$.x')").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid", '"ROWID"', '[oid]', '`_rowid_`'])
def test_implicit_row_positions_are_decided_by_actual_insert(tmp_path, alias):
    db_path = tmp_path / "row-position.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(n integer not null,constraint ck check(n>=1 and n={alias}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where n=rowid").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("alias", ["rowid", "_rowid_", "oid", "ROWID"])
@pytest.mark.parametrize("omitted", [False, True])
def test_declared_rowid_names_are_ordinary_candidate_context(alias, omitted):
    required, values = [("n", "INTEGER")], {"n": 0}
    defaults = []
    if omitted:
        defaults = [(alias, "INTEGER", "2")]
    else:
        required.append((alias, "INTEGER"))
        values[alias] = 2
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(connection, f'n>0 and n="{alias}" and "{alias}"=2', required, values, omitted=defaults)
        assert proposals.derived == [("n", 2)]
        assert proposals.numeric_fallback == []


@pytest.mark.parametrize("member_type", list(guard.JSON_TYPE_MEMBERS))
@pytest.mark.parametrize("reverse", [False, True])
def test_json_type_dependencies_use_sqlite_member_semantics(tmp_path, member_type, reverse):
    dependency = "tag=json_type(doc,'$.x')" if not reverse else "json_type(doc,'$.x')=tag"
    db_path = tmp_path / "json-type-dependency.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(doc text not null,tag text not null,constraint ck check(json_valid(doc) and json_type(doc,'$.x')='{member_type}' and {dependency}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where tag=json_type(doc,'$.x')").fetchone()[0] >= guard.SEED_ROWS


@pytest.mark.parametrize("wrapper", [
    "{} OR 1", "1 OR {}", "NOT ({}) OR 1", "CASE WHEN 1 THEN 1 ELSE {} END",
    "coalesce(({}),1)=0 OR 1", "({})=0 OR 1", "1 BETWEEN 0 AND 2 OR {}",
])
@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("reverse", [False, True])
def test_optional_substring_terms_cannot_rewrite_required_columns(tmp_path, wrapper, equality, reverse):
    left, right = 'substr("a",1,1)', '"b"'
    if reverse:
        left, right = right, left
    expression = wrapper.format(f"{left} {equality} {right}")
    assert list(guard.substring_requirements(expression)) == []
    checks = [expression, "a='A'", "b='R'", "state='ready'"]
    if reverse:
        checks.reverse()
    db_path = tmp_path / "optional-prefix.sqlite"
    ddl = "create table shaped(a text not null,b text not null,state text not null," + ",".join(
        f"constraint ck{index} check({check})" for index, check in enumerate(checks)
    ) + ")"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select a,b,state from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS
        assert set(rows) == {("A", "R", "ready")}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("shape", ["length(a)=2", "a GLOB 'B'", "a NOT GLOB 'A'", "substr(a,1,1)=b"])
@pytest.mark.parametrize("wrapper", [
    "({}) OR 1", "1 OR ({})", "NOT ({}) OR 1", "CASE WHEN 1 THEN 1 ELSE {} END",
    "coalesce(({}),1)=0 OR 1", "({})=0 OR 1",
])
@pytest.mark.parametrize("reverse", [False, True])
def test_only_unconditional_shape_terms_can_force_a_repair(tmp_path, shape, wrapper, reverse):
    expression = wrapper.format(shape)
    checks = [expression, "a='A'", "b='R'", "state='ready'"]
    if reverse:
        checks.reverse()
    ddl = "create table shaped(a text not null,b text not null,state text not null," + ",".join(
        f"constraint ck{index} check({check})" for index, check in enumerate(checks)
    ) + ")"
    db_path = tmp_path / "optional-shapes.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
        proposed = guard.shape_proposals(connection, "state='ready'",
                                          [("a", "TEXT"), ("b", "TEXT"), ("state", "TEXT")],
                                          {"a": "A", "b": "R", "state": "x"},
                                          text_constraints=" AND ".join(f"({check})" for check in checks))
        assert proposed.derived == []
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select * from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {("A", "R", "ready")}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("wrapper", ["NOT ({})", "coalesce(({}),1)=0", "({})=0"])
@pytest.mark.parametrize("reverse", [False, True])
def test_nonidentity_substring_expressions_preserve_valid_bindings(wrapper, reverse):
    term = "b=substr(a,1,1)" if reverse else "substr(a,1,1)=b"
    expression = wrapper.format(term)
    assert list(guard.substring_requirements(expression)) == []
    checks = [expression, "a='A'", "b='R'", "state='ready'"]
    if reverse:
        checks.reverse()
    ddl = "create table shaped(a text not null,b text not null,state text not null," + ",".join(
        f"constraint ck{index} check({check})" for index, check in enumerate(checks)
    ) + ")"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        result = guard.insert_seed_row(connection, "shaped", ddl,
                                       [("a", "TEXT"), ("b", "TEXT"), ("state", "TEXT")],
                                       {"a": "A", "b": "R", "state": "x"})
        assert result == ("", True)
        assert connection.execute("select * from shaped").fetchall() == [("A", "R", "ready")]
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("order", list(itertools.permutations(["a", "b", "c"])))
@pytest.mark.parametrize("bound", [2, 4, 12])
@pytest.mark.parametrize("reverse,strict", itertools.product([False, True], repeat=2))
def test_numeric_rejected_candidates_remain_origins_for_repeat_rows(tmp_path, order, bound, reverse, strict):
    clauses = [f"abs(b)={bound}", "(a>0)+(b>0)+(c>0)=1"]
    if reverse:
        clauses.reverse()
    db_path = tmp_path / "numeric-repeat.sqlite"
    ddl = "create table shaped(" + ",".join(f'"{name}" integer not null' for name in order)
    ddl += f",constraint ck check({' AND '.join(clauses)}))" + (" STRICT" if strict else "")
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("order", list(itertools.permutations(["a", "b", "c"])))
@pytest.mark.parametrize("initial", [(0, 1, 1), (2, 0, 2), (3, 3, 0)])
def test_numeric_progress_reaches_insert_without_resetting_its_budget(order, initial):
    ddl = "create table shaped(" + ",".join(f'"{name}" integer not null' for name in order)
    ddl += ",constraint ck check(abs(b)=4 AND (a>0)+(b>0)+(c>0)=1))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        attempts = []
        connection.set_trace_callback(lambda sql: attempts.append(sql) if sql.startswith('insert into "shaped"') else None)
        values = dict(zip(["a", "b", "c"], initial))
        result = guard.insert_seed_row(connection, "shaped", ddl, [(name, "INTEGER") for name in order], values)
        assert result == ("", True)
        assert len(attempts) == len(set(attempts)) <= guard.SEED_ATTEMPTS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared", ["DATE", "NUMERIC", "DECIMAL", "ANY"])
@pytest.mark.parametrize("reverse", [False, True])
def test_numeric_progress_keeps_required_prefix_semantics_reachable(declared, reverse):
    names = ["a", "c"]
    if reverse:
        names.reverse()
    ddl = "create table shaped(" + ",".join(f"{name} {declared} not null" for name in names)
    ddl += ",constraint ck_c check(length(c)=9 and substr(c,1,4)=a),constraint ck_a check(a in ('R-A0')))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        attempts = []
        connection.set_trace_callback(lambda sql: attempts.append(sql) if sql.startswith('insert into "shaped"') else None)
        result = guard.insert_seed_row(connection, "shaped", ddl, [(name, declared) for name in names],
                                       {name: "x" for name in names})
        assert result == ("", True)
        assert len(attempts) == len(set(attempts)) <= guard.SEED_ATTEMPTS
        assert any("R-A0" in attempt for attempt in attempts)
        assert connection.execute("select a,substr(c,1,4) from shaped").fetchall() == [("R-A0", "R-A0")]
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("path", ["$.x", "$.nested.x", "$.it's"])
@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("reverse", [False, True])
def test_json_missing_path_is_an_alternative_not_a_forced_member(tmp_path, path, equality, reverse):
    quoted = path.replace("'", "''")
    clauses = [f"json_extract(doc,'{quoted}') {equality} 1", f"json_type(doc,'{quoted}') {equality} 'false'"]
    if reverse:
        clauses.reverse()
    expression = "json_valid(doc) AND " + " AND ".join(clauses)
    db_path = tmp_path / "json-missing-path.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(doc text not null,constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select doc from shaped").fetchall()
        if equality == "IS":
            # IS rejects a missing path: keeping the candidate must not skip CHECK.
            assert short and rows == []
        else:
            assert short == {} and len(rows) >= guard.SEED_ROWS
            assert all(connection.execute("select json_type(?,?)", (doc, path)).fetchone()[0] is None for (doc,) in rows)
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("payload", [
    "x=substr(email,5,1)", "substr(email,5,1)=x", "length(email)=2",
    "email glob '[A-Z]*'", "json_extract(email,'$.x')='ready'", "n=9999", "email=x",
])
@pytest.mark.parametrize("wrapper", ["/* {} */", "-- {}\n", "'{}'", '"{}"', '`{}`', '[{}]'])
def test_candidate_extractors_share_opaque_sql_boundaries(payload, wrapper):
    encoded = payload.replace("'", "''") if wrapper == "'{}'" else payload
    expression = wrapper.format(encoded)
    assert list(guard.substring_requirements(expression)) == []
    assert list(guard.sql_matches(guard.JSON_REQUIREMENT, expression)) == []
    assert guard.column_equality_groups(expression, ["email", "x", "n"]) == [["email"], ["x"], ["n"]]
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(connection, expression, [("email", "TEXT"), ("x", "TEXT"), ("n", "INTEGER")], {"email": "seed@example.invalid", "x": "x", "n": 0})
        assert proposals.derived == []
        assert proposals.numeric_fallback == []


@pytest.mark.parametrize("literal", ["'(' || ')'", "'x)''('" , "'/* ) */ -- ('"])
def test_check_body_boundaries_ignore_quoted_and_commented_parentheses(literal):
    expression = f"length({literal})>0 /* ) */ and n>0 -- (\n"
    ddl = f"create table shaped(n integer,constraint ck check({expression}))"
    assert guard.check_expression(ddl, "ck") == expression
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        connection.execute("insert into shaped values(1)")


@pytest.mark.parametrize("path", ["$.x", "$.it's"])
def test_json_literals_keep_sql_comment_markers_and_expression_text(path):
    value = "/* n=9999 */ -- length(email)=2; json_extract(email,'$.x')='bad'"
    quoted_path, quoted_value = path.replace("'", "''"), value.replace("'", "''")
    expression = f"json_extract(doc,'{quoted_path}')='{quoted_value}' and state=json_extract(doc,'{quoted_path}')"
    with sqlite3.connect(":memory:") as connection:
        assignments = guard.json_proposals(connection, expression, ["doc", "state", "email"])
        assert assignments[-1] == (("doc", "{}"),)
        proposals = dict(assignments[0])
        assert set(proposals) == {"doc", "state"}
        assert proposals["state"] == value
        assert connection.execute("select json_extract(?,?)", (proposals["doc"], path)).fetchone()[0] == value


@pytest.mark.parametrize("required,optional,witness", [
    ("json_extract(doc,'$.x')=1", "json_extract(doc,'$.x')=2", '{"x":1}'),
    ("json_type(doc,'$.x')='integer'", "json_type(doc,'$.x')='text'", '{"x":0}'),
])
@pytest.mark.parametrize("wrapper", [
    "{} OR 1", "1 OR {}", "NOT ({}) OR 1", "CASE WHEN 1 THEN 1 ELSE {} END",
    "({})=0 OR 1", "coalesce(({}),1)=0 OR 1", "json_valid(doc) AND likely(({}) OR 1)",
])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
def test_optional_json_clauses_cannot_replace_required_documents(tmp_path, required, optional, witness, wrapper, reverse, separate):
    mandatory = f"(json_valid(doc) AND {required}) IS TRUE"
    terms = [mandatory, wrapper.format(optional)]
    if reverse:
        terms.reverse()
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms)) if separate else f"constraint ck check({' AND '.join(f'({term})' for term in terms)})"
    ddl = f"create table shaped(doc text not null,{checks})"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        connection.execute("insert into shaped values(?)", (witness,))
        assignments = guard.json_proposals(connection, " AND ".join(f"({term})" for term in terms), ["doc"])
        first = dict(assignments[0])["doc"]
        assert connection.execute(f"select {mandatory} from (select ? as doc)", (first,)).fetchone()[0] == 1
    db_path = tmp_path / "required-json.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {mandatory}").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("optional", [
    "json_extract(doc,'invalid')=2",
    "state IS lower(json_extract(doc,'invalid'))",
    "state IS json_type(doc,'invalid')",
])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
def test_dead_json_paths_cannot_abort_other_repairs(tmp_path, optional, reverse, separate):
    terms = [
        "(json_valid(doc) AND json_extract(doc,'$.x')=1) IS TRUE",
        f"CASE WHEN 1 THEN 1 ELSE {optional} END AND state='ready'",
    ]
    if reverse:
        terms.reverse()
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms)) if separate else f"constraint ck check({' AND '.join(f'({term})' for term in terms)})"
    ddl = f"create table shaped(doc text not null,state text not null,{checks})"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        connection.execute("insert into shaped values(?,?)", ('{"x":1}', "ready"))
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]
    db_path = tmp_path / "dead-json-path.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select json_extract(doc,'$.x'),state from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {(1, "ready")}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("branch", [
    "(json_extract(doc,'$.x')=1 OR state='ready')",
    "CASE WHEN state='x' THEN json_extract(doc,'$.x')=1 ELSE 1 END",
])
def test_local_optional_json_comparisons_remain_speculative_candidates(tmp_path, branch):
    expression = "json_valid(doc) AND " + branch
    db_path = tmp_path / "optional-json-alternatives.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(doc text not null,state text not null,constraint ck check({expression}))")
        assignments = guard.json_proposals(connection, expression, ["doc", "state"])
        assert any(dict(assignment).get("doc") == '{"x":1}' for assignment in assignments)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {expression}").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("path", ["invalid", "$["])
def test_invalid_json_candidate_paths_preserve_native_refusal(tmp_path, path):
    expression = f"(json_valid(doc) AND json_extract(doc,'{path}')=1) IS TRUE"
    ddl = f"create table shaped(doc text not null,constraint ck check({expression}))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        with pytest.raises(sqlite3.Error) as native:
            connection.execute("insert into shaped values('{}')")
        objection, settled = guard.insert_seed_row(connection, "shaped", ddl, [("doc", "TEXT")], {"doc": "{}"})
        assert settled and objection == str(native.value)
        assert connection.execute("select count(*) from shaped").fetchone()[0] == 0
    db_path = tmp_path / "invalid-json-path.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] == 0


def test_dead_invalid_json_candidate_does_not_change_opaque_document():
    expression = "CASE WHEN 1 THEN 1 ELSE json_extract(doc,'invalid')=2 END AND state='ready'"
    ddl = f"create table shaped(doc text not null,state text not null,constraint ck check({expression}))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        assert guard.insert_seed_row(connection, "shaped", ddl, [("doc", "TEXT"), ("state", "TEXT")],
                                     {"doc": "opaque", "state": "x"}) == ("", True)
        assert connection.execute("select * from shaped").fetchall() == [("opaque", "ready")]


@pytest.mark.parametrize("wrapper", [
    "(({}))", "({}) IS TRUE", "({})=1", "1=({})", "({}) == TRUE",
    "({}) IS NOT FALSE", "NOT NOT ({})", "likely({})", '\"likely\"({})',
    "unlikely({})", "likelihood(({}),0.25)", "likely((({}) IS TRUE))",
])
@pytest.mark.parametrize("predicate", ["length(a)=2", "a GLOB 'AB'", "substr(a,1,2)=b", "a=b"])
@pytest.mark.parametrize("reverse", [False, True])
def test_required_truth_wrappers_preserve_seedable_shape_contracts(tmp_path, wrapper, predicate, reverse):
    checks = [wrapper.format(predicate), "b='AB'", "state='ready'"]
    if reverse:
        checks.reverse()
    ddl = "create table shaped(a text not null,b text not null,state text not null," + ",".join(
        f"constraint ck{i} check({check})" for i, check in enumerate(checks)
    ) + ")"
    db_path = tmp_path / "required-wrapper.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {predicate} and b='AB' and state='ready'").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("wrapper", [
    "likely({})", "unlikely({})", "likelihood({},0.5)", "({}) IS TRUE", "({})=1", "NOT NOT ({})",
])
@pytest.mark.parametrize("predicate", ["length(a)=2", "a GLOB 'B'", "substr(a,1,1)=b", "a=b"])
def test_truth_wrappers_do_not_make_optional_shapes_required(wrapper, predicate):
    check = wrapper.format(f"({predicate}) OR 1")
    ddl = f"create table shaped(a text,b text,state text,constraint ck check({check}),constraint ready check(state='ready'))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        values = {"a": "A", "b": "R", "state": "x"}
        assert guard.insert_seed_row(connection, "shaped", ddl, [(name, "TEXT") for name in values], values) == ("", True)
        assert connection.execute("select * from shaped").fetchall() == [("A", "R", "ready")]


@pytest.mark.parametrize("wrapper", ["({}) IS FALSE", "({})=0", "NOT ({})", "coalesce(({}),0)=0", "iif(({}),0,1)"])
def test_false_wrappers_do_not_become_required_equalities(wrapper):
    expression = wrapper.format("a=b")
    assert guard.column_equality_groups(expression, ["a", "b"]) == [["a"], ["b"]]


@pytest.mark.parametrize("name,value,wrapper", [("TRUE", 0, "({}) IS TRUE"), ("false", 1, "({}) IS NOT FALSE")])
@pytest.mark.parametrize("predicate", ["length(a)=2", "a GLOB 'B'", "substr(a,1,1)=b", "a=b"])
@pytest.mark.parametrize("omitted,strict", itertools.product([False, True], repeat=2))
def test_declared_boolean_names_are_not_truth_wrappers(name, value, wrapper, predicate, omitted, strict):
    checks = [wrapper.format(predicate), "a='A'", "b='R'", "state='ready'"]
    ddl = f"create table shaped(a text,b text,state text,{name} INTEGER DEFAULT {value}," + ",".join(
        f"constraint ck{i} check({check})" for i, check in enumerate(checks)
    ) + ")" + (" STRICT" if strict else "")
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        values = {"a": "A", "b": "R", "state": "x"}
        required = [(column, "TEXT") for column in values]
        if not omitted:
            values[name] = value
            required.append((name, "INTEGER"))
        assert guard.insert_seed_row(connection, "shaped", ddl, required, values) == ("", True)
        assert connection.execute("select a,b,state from shaped").fetchall() == [("A", "R", "ready")]
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("dependency", ["lower(a)=b", "b=lower(a)", 'b="lower"(a)', "trim(a)=b"])
@pytest.mark.parametrize("separate", [False, True])
def test_independent_glob_witnesses_retain_coordinated_alternatives(tmp_path, order, dependency, separate):
    terms = ["a NOT GLOB '*[^a]*'", "b GLOB 'a'", dependency]
    terms = [terms[i] for i in order]
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms)) if separate else f"constraint ck check({' AND '.join(terms)})"
    ddl = f"create table shaped(a text not null,b text not null,{checks})"
    db_path = tmp_path / "coordinated-glob.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select * from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {("a", "a")}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("declared", ["TEXT", "DATE", "BLOB"])
def test_bare_equality_groups_propagate_values_without_shapes(tmp_path, equality, reverse, declared):
    terms = ["b='A'", f"a {equality} b", f"c {equality} a"]
    if reverse:
        terms.reverse()
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms))
    db_path = tmp_path / "bare-equality.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(a {declared} not null,b {declared} not null,c {declared} not null,{checks})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select * from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {("A", "A", "A")}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("value,declared", [(b"A", "BLOB"), (17, "INTEGER"), (1.5, "REAL"), ("A", "TEXT")])
def test_bare_equality_alternatives_preserve_native_member_storage(value, declared):
    with sqlite3.connect(":memory:") as connection:
        ddl = f"create table shaped(a {declared},b {declared},constraint same check(a=b))"
        connection.execute(ddl)
        values = {"a": guard.representative_value("a", declared), "b": value}
        proposed = guard.shape_proposals(connection, "a=b", [("a", declared), ("b", declared)], values)
        assert (("a", value), ("b", value)) in proposed.semantic_fallback
        originals = {(type(candidate), candidate) for candidate in values.values()}
        assert all((type(candidate), candidate) in originals for assignment in proposed.semantic_fallback for _, candidate in assignment)
        assert guard.insert_seed_row(connection, "shaped", ddl, [("a", declared), ("b", declared)], values) == ("", True)
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("function", ["lower", '"lower"', "trim", "likely"])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("path", ["$.x", "$.it's"])
def test_json_functional_candidates_retain_their_constructed_document(tmp_path, function, reverse, separate, path):
    path_sql = path.replace("'", "''")
    member = f"json_extract(doc,'{path_sql}')"
    dependency = f"state IS {function}({member})" if not reverse else f"{function}({member}) IS state"
    terms = [f"json_valid(doc) AND {member}='ready'", f"json_valid(doc) AND {dependency}"]
    if reverse:
        terms.reverse()
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms)) if separate else f"constraint ck check({' AND '.join(terms)})"
    ddl = f"create table shaped(doc text not null,state text not null,{checks})"
    db_path = tmp_path / "functional-json.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
        assignments = guard.json_proposals(connection, terms[0], ["doc", "state"], text_constraints=" AND ".join(f"({term})" for term in terms))
        for assignment in assignments:
            row = dict(assignment)
            if "state" in row:
                assert connection.execute("select json_extract(?,?)", (row["doc"], path)).fetchone()[0] == row["state"]
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select state from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {("ready",)}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("producer", ["glob", "json"])
@pytest.mark.parametrize("reverse,separate", itertools.product([False, True], repeat=2))
def test_semantic_candidates_follow_dependencies_without_poisoning_disconnected_columns(tmp_path, producer, reverse, separate):
    if producer == "glob":
        source = "code GLOB 'ready'"
        dependency = "state=lower(code)"
    else:
        source = "json_valid(code) AND json_extract(code,'$.x')='ready'"
        dependency = "json_valid(code) AND state IS lower(json_extract(code,'$.x'))"
    terms = [source, dependency, "label=trim(state)", "json_extract(payload_json,'$.x')=1", "instr(email,'@')>0"]
    if reverse:
        terms.reverse()
    # JSON remains guarded when its dependency precedes its defining CHECK.
    checks = ",".join(f"constraint ck{i} check({term})" for i, term in enumerate(terms)) if separate else f"constraint ck check({' AND '.join(terms)})"
    ddl = f"create table shaped(code text not null,state text not null,label text not null,payload_json text not null,email text not null,{checks})"
    db_path = tmp_path / "semantic-recipients.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select state,label,json_valid(payload_json),instr(email,'@')>0 from shaped").fetchall()
        assert len(rows) >= guard.SEED_ROWS and set(rows) == {("ready", "ready", 1, 1)}
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("note", [
    "1 /* x=substr(email_address,5,1) */",
    "1 -- x=substr(email_address,5,1)\n",
    "length('x=substr(email_address,5,1)')>0",
    "length('quoted '' x=substr(email_address,5,1)')>0",
])
@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_dependency_text_is_not_executable_sql(tmp_path, note, order):
    clauses = ["constraint ck_email check(instr(email_address,'@')>1)", f"constraint ck_note check({note})", "constraint ck_state check(state='ready')"]
    db_path = tmp_path / "opaque-sql.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(email_address text not null,x text not null,state text not null,{','.join(clauses[i] for i in order)})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where instr(email_address,'@')>1 and state='ready'").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("unavailable", [(), ("generated",)])
def test_shape_candidates_keep_derived_repairs_separate_from_numeric_guesses(unavailable):
    expression = "length(code)=4 and n>0" + (" and generated=0" if unavailable else "")
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(
            connection, expression, [("code", "TEXT"), ("n", "INTEGER")],
            {"code": "x000", "n": 0}, unavailable=unavailable,
        )
        if unavailable:
            assert proposals.derived == []
            assert any(("n", 1) in assignment for assignment in proposals.numeric_fallback)
        else:
            assert proposals.derived == [("n", 1)]
            assert proposals.numeric_fallback == []
        shaped = guard.shape_proposals(
            connection, expression, [("code", "TEXT"), ("n", "INTEGER")],
            {"code": "x", "n": 0}, unavailable=unavailable,
        )
        assert ("code", "x000") in shaped.derived
        assert not any(name == "code" for assignment in shaped.numeric_fallback for name, _ in assignment)


@pytest.mark.parametrize("pattern,width", [("[A-Z]*", 4), ("*[A-Z]", 4), ("[A-Z]*[A-Z]", 4), ("*[A-Z]*", 4), ("[A-Z]???", 4), ("[A-Z][A-Z][A-Z][A-Z]", 4), ("*", 0), ("*", guard.SEED_TEXT_LIMIT)])
@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("alphabet", ["A-Z", "0-9A-Z"])
def test_text_shape_composition_preserves_every_requirement(tmp_path, pattern, width, order, separate, alphabet):
    requirements = [f"length(code) = {width}", f"code glob '{pattern}'", f"code not glob '*[^{alphabet}]*'"]
    expressions = [requirements[index] for index in order]
    if not separate:
        expressions = [" and ".join(expressions)]
    checks = ", ".join(f"constraint ck_{index} check ({expression})" for index, expression in enumerate(expressions))
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, code text not null, {checks})")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize(
    "columns,clauses",
    [
        (columns, clauses)
        for names in [("low", "high"), ("low", "middle", "high")]
        for columns in itertools.permutations(names)
        for clauses in itertools.permutations(["low > 0", *(f"{right} >= {left}" for left, right in itertools.pairwise(names))])
    ],
)
def test_numeric_repairs_coordinate_columns_without_order_dependence(tmp_path, columns, clauses):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        declared = ", ".join(f'"{name}" integer not null' for name in columns)
        connection.execute(f"create table shaped (id integer primary key, {declared}, constraint ck_counts check ({' and '.join(clauses)}))")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


def test_joint_numeric_evaluation_has_a_finite_budget(monkeypatch):
    required = [(f"n{index}", "INTEGER") for index in range(6)]
    values = {name: 0 for name, _ in required}
    expression = " and ".join(f"{name} > 0" for name in values) + " and n5 < 0"
    statements = []
    with sqlite3.connect(":memory:") as connection:
        connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            candidate = connect(*args, **kwargs)
            candidate.set_trace_callback(statements.append)
            return candidate
        monkeypatch.setattr(guard.sqlite3, "connect", traced_connect)
        proposals = guard.shape_proposals(connection, expression, required, values)
        assert proposals.derived == []
        assert len(proposals.numeric_fallback) == guard.SEED_ATTEMPTS
        connection.execute("create table rejected(" + ",".join(f"{name} integer" for name in values)
                           + f",check({expression}))")
        for assignment in proposals.numeric_fallback:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute("insert into rejected values(?,?,?,?,?,?)", [value for _, value in assignment])
    evaluations = [statement for statement in statements if statement.startswith("select coalesce(cast(")]
    assert len(evaluations) == guard.SEED_ATTEMPTS


@pytest.mark.parametrize("changed", range(6))
def test_joint_numeric_search_preserves_single_column_repairs(changed):
    required = [(f"n{index}", "INTEGER") for index in range(6)]
    values = {name: 0 for name, _ in required}
    expression = " and ".join(f"{name} >= 0" for name in values) + f" and n{changed} > 0"
    with sqlite3.connect(":memory:") as connection:
        assert guard.shape_proposals(connection, expression, required, values).derived == [(f"n{changed}", 1)]


@pytest.mark.parametrize("clauses", list(itertools.permutations(["n0 in (0)", "n1 in (2)", "n2 in (4)"])))
def test_cartesian_repairs_keep_budget_for_unranked_predicates(tmp_path, clauses):
    db_path = tmp_path / "cartesian.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(n0 integer not null,n1 integer not null,n2 integer not null,constraint ck check({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where n0=0 and n1=2 and n2=4").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("count", [16, 25, 30])
@pytest.mark.parametrize("reverse_columns,reverse_clauses", itertools.product([False, True], repeat=2))
def test_numeric_only_fallback_remains_available_until_insert_budget(tmp_path, count, reverse_columns, reverse_clauses):
    columns = [f'n{index} integer not null' for index in range(count)]
    clauses = ['g=0', *(f'n{index}>=0' for index in range(count-1)), f'n{count-1}=1']
    if reverse_columns:
        columns.reverse()
    if reverse_clauses:
        clauses.reverse()
    db_path = tmp_path / "fallback.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(columns)},g integer generated always as(0),constraint ck check({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("reverse_columns,not_null", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("order", list(itertools.permutations(range(2))))
@pytest.mark.parametrize("case_guard", [False, True])
def test_json_and_generic_literals_preserve_each_others_repairs(tmp_path, reverse_columns, not_null, order, case_guard):
    names = ['doc', 'state']
    if reverse_columns:
        names.reverse()
    json_check = "json_extract(doc,'$.x')=1"
    if case_guard:
        json_check = f"case when json_valid(doc) then {json_check} else 0 end"
    clauses = [json_check, "state='ready'"]
    columns = ','.join(f'{name} text' + (' not null' if not_null else '') for name in names)
    expression = ' and '.join(clauses[index] for index in order)
    if not case_guard:
        expression = f'json_valid(doc) and ({expression})'
    ddl = f"create table shaped({columns},constraint ck check({expression}))"
    db_path = tmp_path / "json.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
        assert guard.insert_seed_row(connection, 'shaped', ddl, [(name,'TEXT') for name in names], dict.fromkeys(names, 'x')) == ('', True)
        assert connection.execute("select json_extract(doc,'$.x'),state from shaped").fetchall() == [(1,'ready')]
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where doc is not null and state is not null").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("strict,name,check", [
    (True, "value", "lower(value)=value"),
    (True, "payload_json", "json_valid(payload_json)"),
    (True, "value", "typeof(value)='integer' and value>0"),
    (False, "value", "typeof(value)='integer' and value>0"),
])
def test_strict_any_supports_semantic_and_integer_seeds(tmp_path, strict, name, check):
    db_path = tmp_path / "strict.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({name} ANY not null,constraint ck check({check}))" + (' STRICT' if strict else ''))
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("strict,omitted", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("literal", ["'000123'", "'1.25'", "1", "1.25", "x'313233'", "NULL"])
def test_any_candidate_context_matches_native_table_mode(strict, omitted, literal):
    with sqlite3.connect(":memory:") as connection:
        suffix = ' STRICT' if strict else ''
        connection.execute('create table oracle(value ANY)' + suffix)
        connection.execute(f'insert into oracle values({literal})')
        storage, quoted = connection.execute('select typeof(value),quote(value) from oracle').fetchone()
        expected_quote = quoted.replace("'", "''")
        ddl = f"create table shaped(n integer not null,value ANY default ({literal}),constraint ck check(n>0 and typeof(value)='{storage}' and quote(value)='{expected_quote}')){suffix}"
        connection.execute(ddl)
        required,values = [('n','INTEGER')],{'n':0}
        if not omitted:
            required.append(('value','ANY'))
            values['value'] = connection.execute(f'select {literal}').fetchone()[0]
        assert guard.insert_seed_row(connection,'shaped',ddl,required,values) == ('',True)
        assert connection.execute('select typeof(value),quote(value) from shaped').fetchone() == (storage,quoted)


@pytest.mark.parametrize("context", ["rowid>=1", "_rowid_>=1", "oid>=1", "g=0", "changes()>=0", "total_changes()>=0"])
@pytest.mark.parametrize("reverse_columns,reverse_clauses", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("count", [2, 3])
def test_target_context_receives_atomic_joint_assignments(tmp_path, context, reverse_columns, reverse_clauses, count):
    names = [f'n{i}' for i in range(count)]
    clauses = [context, *(f'n{i}={i+1}' for i in range(count))]
    if reverse_columns:
        names.reverse()
    if reverse_clauses:
        clauses.reverse()
    expression = ' and '.join(clauses)
    db_path = tmp_path / 'joint-target.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},g integer generated always as(0),constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        expected = ' and '.join(f'n{i}={i+1}' for i in range(count))
        assert connection.execute(f"select count(*) from shaped where {expected}").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


def test_target_context_candidate_is_applied_in_one_insert():
    with sqlite3.connect(':memory:') as connection:
        ddl = 'create table shaped(a integer,b integer,constraint ck check(rowid>=1 and a=1 and b=2))'
        connection.execute(ddl)
        attempts = []
        connection.set_trace_callback(lambda statement: attempts.append(statement) if statement.startswith('insert into "shaped"') else None)
        assert guard.insert_seed_row(connection, 'shaped', ddl, [('a', 'INTEGER'), ('b', 'INTEGER')], {'a': 0, 'b': 0}) == ('', True)
        assert len(attempts) == 2
        assert connection.execute('select a,b from shaped').fetchall() == [(1, 2)]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_repairs_can_return_to_a_cell_after_other_columns_change(order):
    clauses = ["code not glob '*[^a]*'", "length(code)=0", "state='ready'"]
    with sqlite3.connect(':memory:') as connection:
        ddl = f"create table shaped(code text,state text,constraint ck check({' and '.join(clauses[index] for index in order)}))"
        connection.execute(ddl)
        attempts = []
        connection.set_trace_callback(lambda statement: attempts.append(statement) if statement.startswith('insert into "shaped"') else None)
        assert guard.insert_seed_row(connection, 'shaped', ddl, [('code', 'TEXT'), ('state', 'TEXT')], {'code': 'x', 'state': 'x'}) == ('', True)
        assert connection.execute('select code,state from shaped').fetchall() == [('', 'ready')]
        assert len(attempts) == len(set(attempts))
        assert len(attempts) <= guard.SEED_ATTEMPTS


@pytest.mark.parametrize("bound", range(1, 13))
@pytest.mark.parametrize("reverse_columns,reverse_clauses", itertools.product([False, True], repeat=2))
def test_later_sparse_alternatives_reach_real_seed_rows(tmp_path, bound, reverse_columns, reverse_clauses):
    names = ['n0', 'n1']
    clauses = [f'abs(n0)={bound}', 'n1 in (' + ','.join(map(str, range(bound))) + ')']
    if reverse_columns:
        names.reverse()
    if reverse_clauses:
        clauses.reverse()
    db_path = tmp_path / 'later-sparse.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},constraint ck check({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("count,alternatives", [(2, 14), (5, 5), (17, 1)])
def test_sparse_family_within_reserved_budget_is_not_diluted(count, alternatives):
    domains = [list(range(alternatives + 1)) for _ in range(count)]
    current = (0,) * count
    prefix = set(itertools.islice(guard.numeric_assignments(domains, current, domains[0]), guard.SEED_ATTEMPTS))
    assert all((*current[:index], value, *current[index+1:]) in prefix for index in range(count) for value in domains[index][1:])


@pytest.mark.parametrize("negated,pattern", [(True, '*[^a]*'), (False, 'a*'), (False, '*z'), (True, '*[0-9]*'), (False, "a'*"), (True, "*[^a']*")])
@pytest.mark.parametrize("reverse_columns,reverse_clauses", itertools.product([False, True], repeat=2))
def test_glob_patterns_remain_in_their_semantic_candidate_owner(tmp_path, negated, pattern, reverse_columns, reverse_clauses):
    term = "code " + ('not ' if negated else '') + "glob '" + pattern.replace("'", "''") + "'"
    clauses, names = [term, "state='ready'"], ['code', 'state']
    if reverse_clauses:
        clauses.reverse()
    if reverse_columns:
        names.reverse()
    expression = ' and '.join(clauses)
    assert guard.check_proposals(expression, names) == [('state', 'ready')]
    db_path = tmp_path / 'glob-literal.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' text not null' for name in names)},constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f'select count(*) from shaped where {expression}').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_equal_columns_share_composed_glob_witnesses(tmp_path, equality, reverse, separate, order):
    left, right = ('"code value"', 'state') if not reverse else ('state', '"code value"')
    clauses = [f"{left} {equality} {right}", "\"code value\" glob 'A*'", "length(state)=4 and state glob '*Z' and state not glob '*0*'"]
    clauses = [clauses[index] for index in order]
    checks = ','.join(f'constraint ck{index} check({clause})' for index, clause in enumerate(clauses)) if separate else f"constraint ck check({' and '.join(clauses)})"
    db_path = tmp_path / 'equal-shapes.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f'create table shaped("code value" text not null,state text not null,{checks})')
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped where "code value"=state').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("pattern", ["ready", "r*dy", "it''s ready"])
def test_glob_equalities_propagate_through_chains_and_cycles(tmp_path, order, pattern):
    names = ['code', 'state', 'label']
    equalities = [f'{names[index]}={names[(index+1)%3]}' for index in order]
    db_path = tmp_path / 'shape-cycle.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' text not null' for name in reversed(names))},constraint ck check(code glob '{pattern}' and {' and '.join(equalities)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped where code=state and state=label').fetchone()[0] >= guard.SEED_ROWS


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
@pytest.mark.parametrize("reverse", [False, True])
def test_equal_shape_groups_survive_substring_repairs(tmp_path, order, reverse):
    equalities = ['state=code', 'substr(code,1,2)=source'] if not reverse else ['code=state', 'source=substr(code,1,2)']
    clauses = ["source glob 'AB'", "code glob 'A*'", *equalities]
    db_path = tmp_path / 'equal-substring.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(source text not null,state text not null,code text not null,constraint ck check({' and '.join(clauses[index] for index in order)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped where state=code and substr(code,1,2)=source').fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("reverse_checks,reverse_columns", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("position", [1, 10, 20])
def test_candidate_progress_belongs_to_the_failing_check(tmp_path, reverse_checks, reverse_columns, position):
    kinds = [f"'kind{i}'" for i in range(position)] + ["'target'"] + [f"'kind{i}'" for i in range(position, 40)]
    states = ["'ready'"] + [f"'state{i}'" for i in range(60)]
    checks = [f"constraint ck_kind check(kind in ({','.join(kinds)}) and kind='target')", f"constraint ck_state check(state in ({','.join(states)}) and state='ready')"]
    columns = ['kind', 'state']
    if reverse_checks:
        checks.reverse()
    if reverse_columns:
        columns.reverse()
    db_path = tmp_path / 'check-progress.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' text not null' for name in columns)},{','.join(checks)})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped where kind='target' and state='ready'").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute('pragma integrity_check').fetchall() == [('ok',)]


@pytest.mark.parametrize("expression", ['code=state()', 'state()=code', 'code=t.state', 't.code=state'])
def test_function_and_qualified_operands_are_not_bare_column_aliases(expression):
    assert guard.column_equality_groups(expression, ['code', 'state', 't']) == [['code'], ['state'], ['t']]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("style", ['"{}"', '`{}`', '[{}]'])
def test_equal_shape_groups_preserve_valid_common_values_and_identifier_identity(strict, style):
    source, target = style.format('Source Value'), style.format('Target Value')
    expression = f"{source} glob 'r*' and {target} IS {source}"
    required = [('Source Value', 'TEXT'), ('Target Value', 'ANY' if strict else 'DATE')]
    values = {'Source Value': 'ready', 'Target Value': 'ready'}
    with sqlite3.connect(':memory:') as connection:
        assert guard.shape_proposals(connection, expression, required, values, strict=strict).derived == []
        values['Target Value'] = 'x'
        assert guard.shape_proposals(connection, expression, required, values, strict=strict).derived == [('Target Value', 'ready')]


def test_equal_shape_groups_do_not_promote_strict_numeric_columns_to_text():
    with sqlite3.connect(':memory:') as connection:
        proposals = guard.shape_proposals(connection, "code glob 'ready' and n=code", [('code', 'TEXT'), ('n', 'INTEGER')], {'code': 'x', 'n': 0}, strict=True)
        assert proposals.derived == [('code', 'ready')]


@pytest.mark.parametrize("reverse_checks,reverse_columns", itertools.product([False, True], repeat=2))
def test_numeric_fallback_progress_is_scoped_to_each_check(tmp_path, reverse_checks, reverse_columns):
    checks = ['constraint first check(g=0 and a=1 and b=2)', 'constraint second check(g=0 and c=3 and d=4)']
    names = ['a', 'b', 'c', 'd']
    if reverse_checks:
        checks.reverse()
    if reverse_columns:
        names.reverse()
    db_path = tmp_path / 'numeric-check-progress.sqlite'
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(name+' integer not null' for name in names)},g integer generated always as(0),{','.join(checks)})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute('select count(*) from shaped where a=1 and b=2 and c=3 and d=4').fetchone()[0] >= guard.SEED_ROWS


def test_numeric_fallback_exhaustion_stays_visible(tmp_path):
    db_path = tmp_path / "exhausted.sqlite"
    with sqlite3.connect(db_path) as connection:
        columns = ','.join(f'n{index} integer not null' for index in range(30))
        clauses = ' and '.join(['g=0', *(f'n{index}>=0' for index in range(29)), 'n29=1', 'n29<0'])
        connection.execute(f"create table shaped({columns},g integer generated always as(0),constraint ck check({clauses}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short
    assert all('60 attempts did not produce a row' in reason for reason in short.values())


@pytest.mark.parametrize("member_type", list(guard.JSON_TYPE_MEMBERS))
def test_json_member_literals_stay_in_the_semantic_source(member_type):
    expression = f"json_valid(doc) and json_type(doc,'$.member')='{member_type}' and state='ready'"
    proposals = guard.check_proposals(expression, ['doc', 'state'])
    assert proposals
    assert {value for _, value in proposals} == {'ready'}


def test_strict_mode_comes_from_table_metadata():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("create table ordinary(value ANY default('STRICT'))")
        connection.execute("create table typed(value ANY) STRICT")
        assert not guard.is_strict_table(connection, 'ordinary')
        assert guard.is_strict_table(connection, 'typed')
        assert guard.sqlite_affinity('ANY') == 'NUMERIC'
        assert guard.sqlite_affinity('ANY', strict=True) == 'BLOB'


def test_strict_datatype_rejection_does_not_escape_candidate_evaluation():
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(connection, 'n>0', [('n','INTEGER'),('other','INTEGER')], {'n':0,'other':'not an integer'}, strict=True)
        assert proposals.derived == []
        assert proposals.numeric_fallback == []


@pytest.mark.parametrize("count", [2, 5, 16])
@pytest.mark.parametrize("dense,reverse", itertools.product([False, True], repeat=2))
def test_numeric_search_preserves_sparse_and_dense_consumers(tmp_path, count, dense, reverse):
    names = [f"n{index}" for index in range(count)]
    terms = [f"({name}>0)" for name in names]
    if reverse:
        names.reverse()
    positive = count - 1 if dense else 1
    db_path = tmp_path / "sparse.sqlite"
    with sqlite3.connect(db_path) as connection:
        columns = ','.join(f'{name} integer not null' for name in names)
        connection.execute(f"create table shaped({columns},constraint ck check({'+'.join(terms)}={positive}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared", ["", "TEXT"])
@pytest.mark.parametrize("changed", range(16))
def test_sparse_candidates_keep_turns_at_every_column_position(declared, changed):
    # Extra TEXT context cannot change the numeric scheduling order.
    required = [(f"n{index}", "INTEGER") for index in range(16)] + [("label", declared)]
    values = {name: 0 for name, _ in required}
    values["label"] = "held"
    expression = '+'.join(f'(n{index}>0)' for index in range(16)) + f'=1 and abs(n{changed})>0'
    with sqlite3.connect(":memory:") as connection:
        assert guard.shape_proposals(connection, expression, required, values).derived == [(f"n{changed}", 1)]


@pytest.mark.parametrize("declared", ["", "TEXT", "DATE", "DECIMAL", "ANY", "JSON", "UUID"])
@pytest.mark.parametrize("name,check", [
    ("payload_json", "json_valid(payload_json) and json_type(payload_json)='object'"),
    ("created_at", "datetime(created_at) is not null"),
    ("elapsed_time", "datetime(elapsed_time) is not null"),
    ("email_address", "email_address like '%@%'"),
    ("label", "typeof(label)='text'"),
])
def test_open_storage_columns_retain_semantic_seeds(tmp_path, declared, name, check):
    assert guard.representative_value(name, declared) == guard.representative_value(name, "TEXT")
    db_path = tmp_path / "semantic.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({name} {declared} not null,constraint ck check({check}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared,expected", [("BLOB", b""), ("INTEGER", 0), ("REAL", 0), ("NUMERIC", 0), ("BOOLEAN", 0)])
def test_explicit_storage_defaults_still_take_precedence(declared, expected):
    for name in ["payload_json", "created_at", "elapsed_time", "email_address", "label"]:
        assert guard.representative_value(name, declared) == expected


@pytest.mark.parametrize("keyword", ["and", "or", "between", "case", "end"])
@pytest.mark.parametrize("placement", ["value${}", "{}$value", "value${}$tail"])
@pytest.mark.parametrize("kind", ["glob", "length", "substring", "equality", "json", "literal", "numeric"])
def test_complete_unquoted_identifiers_survive_every_candidate_owner(tmp_path, keyword, placement, kind):
    name = placement.format(keyword)
    expressions = {
        "glob": f"{name} GLOB 'ready'",
        "length": f"length({name})=5",
        "substring": f"substr({name},1,5)=peer",
        "equality": f"{name}=peer",
        "json": f"json_valid({name}) AND json_extract({name},'$.x')='ready'",
        "literal": f"{name}='ready'",
        "numeric": f"{name}=2",
    }
    # Real control tokens still have their meaning beside complete identifiers.
    conjunction = f"n BETWEEN 0 AND 2 AND {name} GLOB 'A' AND CASE WHEN n=0 THEN 1 ELSE 0 END"
    assert list(guard.conjunctive_terms(conjunction)) == [
        "n BETWEEN 0 AND 2", f"{name} GLOB 'A'", "CASE WHEN n=0 THEN 1 ELSE 0 END",
    ]
    predicate = expressions[kind]
    declared = "INTEGER" if kind == "numeric" else "TEXT"
    witness = 2 if kind == "numeric" else '{"x":"ready"}' if kind == "json" else "ready"
    ddl = f"create table shaped({name} {declared} not null,peer text not null,state text not null,constraint ck check({predicate}),constraint p check(peer='ready'),constraint s check(state='ready'))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        connection.execute("insert into shaped values(?,?,?)", (witness, "ready", "ready"))
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]
    db_path = tmp_path / "complete-identifier.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(ddl)
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {predicate} AND peer='ready' AND state='ready'").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("quote", ['"', '`', '['])
@pytest.mark.parametrize("name", ["has space", "has-dash", "123", "n=999", "length(x)=2", "has\"quote", "has`quote", "has'apostrophe", "中文", "a/*comment*/b"])
@pytest.mark.parametrize("kind", ["literal", "shape", "numeric", "json", "substring"])
def test_quoted_column_identity_survives_all_candidate_owners(tmp_path, quote, name, kind):
    def quoted(value):
        return '[' + value + ']' if quote == '[' else quote + value.replace(quote, quote * 2) + quote

    column, source = quoted(name), quoted("source " + name)
    declared = "INTEGER" if kind == "numeric" else "TEXT"
    expressions = {
        "literal": f"{column}='ready'",
        "shape": f"length({column})=4 and {column} glob 'A*' and {column} glob '*Z'",
        "numeric": f"{column}>0 and {column}=2",
        "json": f"json_valid({source}) and json_extract({source},'$.x')='ready' and {column}=json_extract({source},'$.x')",
        "substring": f"{source}='ready' and length({column})=8 and substr({column},1,5)={source}",
    }
    expression = expressions[kind]
    db_path = tmp_path / "quoted.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({column} {declared} not null,{source} TEXT not null,constraint ck check({expression}))")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {expression}").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("quote", ['"', '`', '['])
def test_quoted_identifiers_are_tokens_not_literal_or_code_sources(quote):
    name = "json_extract(other,'$.x')='bad'; n=999"
    column = '[' + name + ']' if quote == '[' else quote + name.replace(quote, quote * 2) + quote
    expression = column + "='ready'"
    assert guard.check_proposals(expression, [name, "other", "n"]) == [(name, "ready")]
    with sqlite3.connect(":memory:") as connection:
        shaped = guard.shape_proposals(connection, expression, [(name, "TEXT"), ("other", "TEXT"), ("n", "INTEGER")], {name: "x", "other": "held", "n": 0})
        assert shaped.derived == []
        assert shaped.numeric_fallback == []
        assert guard.json_proposals(connection, expression, [name, "other", "n"]) == []


@pytest.mark.parametrize("payload", ['"', '`', '[', "'", '"n=999', '`n=999', '[n=999'])
@pytest.mark.parametrize("kind", ["literal", "json", "substring", "numeric"])
def test_opaque_delimiters_cannot_consume_later_identifiers(tmp_path, payload, kind):
    prefix = "length('" + payload.replace("'", "''") + "')>0 AND "
    column, source = '"has space"', '"source space"'
    expressions = {
        "literal": f"{column}='ready'",
        "json": f"json_valid({source}) and json_extract({source},'$.x')='ready' and {column}=json_extract({source},'$.x')",
        "substring": f"{source}='ready' and length({column})=8 and substr({column},1,5)={source}",
        "numeric": f"{column}=2",
    }
    declared = "INTEGER" if kind == "numeric" else "TEXT"
    expression = prefix + expressions[kind]
    db_path = tmp_path / "delimiters.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({column} {declared} not null,{source} text not null,constraint ck check({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"select count(*) from shaped where {expression}").fetchone()[0] >= guard.SEED_ROWS


@pytest.mark.parametrize("name", ["has space", 'has"quote', "has`quote", "/* not a comment */", "(has parentheses)", "", "has\nnewline"])
def test_identifier_round_trip_in_constraints_references_and_repetitions(tmp_path, name):
    def quoted(value):
        return '"' + value.replace('"', '""') + '"'

    parent, child = quoted("parent " + name), quoted("child " + name)
    key, foreign = quoted("key " + name), quoted("foreign " + name)
    column, constraint = quoted(name), quoted("check " + name)
    db_path = tmp_path / "references.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table {parent}({key} text primary key,{column} text not null,constraint {constraint} check({column}='ready'))")
        connection.execute(f"create table {child}({foreign} text not null references {parent}({key}),{column} text not null,constraint {constraint} check({column}='ready'))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("pragma foreign_key_check").fetchall() == []
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]
        for table in (parent, child):
            assert connection.execute(f"select count(*) from {table} where {column}='ready'").fetchone()[0] >= guard.SEED_ROWS
        assert guard.seeded_rows_prove_repetition(connection, "child " + name, [name]) == ""


@pytest.mark.parametrize("name,quoted", [
    (name, quoted)
    for name in ["random", "changes", "total_changes", "last_insert_rowid", "current_timestamp"]
    for quoted in ([True] if name == "current_timestamp" else [False, True])
])
def test_function_named_columns_are_not_function_invocations(tmp_path, name, quoted):
    column = f'"{name}"' if quoted else name
    db_path = tmp_path / "names.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({column} integer not null,a integer not null,constraint ck check({column}=1 and a=2))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("extra", [
    " and 'random changes current_timestamp' != ''",
    " /* random() changes() current_timestamp */",
    " -- random() changes() current_timestamp\n",
])
def test_context_screen_ignores_literal_and_comment_contents(extra):
    with sqlite3.connect(":memory:") as connection:
        proposals = guard.shape_proposals(connection, "a=1 and b=2" + extra, [("a", "INTEGER"), ("b", "INTEGER")], {"a": 0, "b": 0}).derived
        assert proposals == [("a", 1), ("b", 2)]


@pytest.mark.parametrize("function", ["changes()", '"changes"()', "changes /* note */ ()", "current_timestamp", "CURRENT_DATE", "current_time"])
@pytest.mark.parametrize("in_default", [False, True])
def test_compiled_context_functions_defer_without_evaluating(monkeypatch, function, in_default):
    connect = sqlite3.connect
    statements = []
    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    with connect(":memory:") as connection:
        monkeypatch.setattr(guard.sqlite3, "connect", traced_connect)
        expression = "n>0" if in_default else f"n>0 and ({function}) is not null"
        omitted = [("tag", "TEXT", function)] if in_default else []
        proposals = guard.shape_proposals(connection, expression, [("n", "INTEGER")], {"n": 0}, omitted=omitted)
        assert proposals.derived == []
        assert any(("n", 1) in assignment for assignment in proposals.numeric_fallback)
    assert not any(statement.startswith("select coalesce(cast(") for statement in statements)


@pytest.mark.parametrize("default", ["'random'", "'current_timestamp'", "1 /* changes() */", "1 -- random()\n"])
def test_default_literals_and_comments_do_not_disable_joint_evaluation(default):
    with sqlite3.connect(":memory:") as connection:
        assert guard.shape_proposals(connection, "a=1 and b=2", [("a", "INTEGER"), ("b", "INTEGER")], {"a": 0, "b": 0}, omitted=[("tag", "TEXT", default)]).derived == [("a", 1), ("b", 2)]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("separate", [False, True])
@pytest.mark.parametrize("patterns", [("A*", "*Z"), ("[A-C]*", "*[Y-Z]"), ("*A*", "*Z*"), ("\u00e9*", "*]")])
def test_positive_globs_are_composed_in_every_check_order(tmp_path, order, separate, patterns):
    requirements = ["length(code) = 4", *(f"code glob '{pattern}'" for pattern in patterns)]
    expressions = [requirements[index] for index in order]
    if not separate:
        expressions = [" and ".join(expressions)]
    checks = ", ".join(f"constraint ck_{index} check ({expression})" for index, expression in enumerate(expressions))
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, code text not null, {checks})")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("pattern", ["[]]", "[^]]", "[]-]", "[-]", "[[]", "[]a]", "[a-]", "[\u00e9]", "?[]]*"])
def test_glob_classes_follow_sqlite_token_semantics(tmp_path, pattern):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, code text not null, constraint ck_code check (code glob '{pattern}'))")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("negate_left,negate_right", itertools.product([False, True], repeat=2))
def test_glob_intersection_matches_sqlite_over_a_finite_domain(negate_left, negate_right):
    alphabet = "ab[]-"
    patterns = ("*", "a*", "*b", "?a", "[ab]", "[]]", "[^]]", "[[]", "[-a]", "a**b", "[a-]", "[", "[]", "[^]")
    with sqlite3.connect(":memory:") as connection:
        connection.execute("create table domain (value text, width integer)")
        connection.executemany("insert into domain values (?, ?)", [
            ("".join(chars), width) for width in range(4) for chars in itertools.product(alphabet, repeat=width)
        ])
        for left, right, width in itertools.product(patterns, patterns, range(4)):
            expected = connection.execute(
                f"select value from domain where width = ? and value {'not ' if negate_left else ''}glob ? and value {'not ' if negate_right else ''}glob ? limit 1",
                (width, left, right),
            ).fetchone()
            patterns = tuple(pattern for pattern, negated in [(left, negate_left), (right, negate_right)] if not negated)
            excluded = tuple(pattern for pattern, negated in [(left, negate_left), (right, negate_right)] if negated)
            witness = guard.glob_witness(connection, patterns, excluded=excluded, width=width, alphabet=alphabet)
            assert (witness is None) == (expected is None), (left, right, width, witness)
            if witness is not None:
                assert len(witness) == width
                assert connection.execute(
                    f"select ? {'not ' if negate_left else ''}glob ? and ? {'not ' if negate_right else ''}glob ?",
                    (witness, left, witness, right),
                ).fetchone()[0]


def test_glob_search_has_explicit_width_pattern_and_state_limits(monkeypatch):
    with sqlite3.connect(":memory:") as connection:
        assert guard.glob_witness(connection, ("*",), width=guard.SEED_TEXT_LIMIT + 1) is None
        assert guard.glob_witness(connection, ("a" * (guard.SEED_TEXT_LIMIT + 1),)) is None
        assert guard.glob_witness(connection, (), excluded=("a" * (guard.SEED_TEXT_LIMIT + 1),)) is None
        monkeypatch.setattr(guard, "SEED_GLOB_STATES", 3)
        assert guard.glob_witness(connection, ("????",)) is None


@pytest.mark.parametrize("patterns", [("A*", "B*"), ("[]", "*"), ("[", "*")])
def test_unsatisfiable_glob_intersections_remain_visible_failures(tmp_path, patterns):
    db_path = tmp_path / "vibe.sqlite"
    expression = " and ".join(f"code glob '{pattern}'" for pattern in patterns)
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (code text not null, constraint ck_code check ({expression}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert "ck_code" in short["shaped"]


@pytest.mark.parametrize("count", [5, 6, 16])
@pytest.mark.parametrize("bound", [0, 7, -7])
@pytest.mark.parametrize("reverse", [False, True])
def test_joint_boundary_repairs_are_not_starved_by_cartesian_prefix(tmp_path, count, bound, reverse):
    names = [f"n{index}" for index in range(count)]
    clauses = [f"{name} > {bound}" if bound >= 0 else f"{name} < {bound}" for name in names]
    if reverse:
        names.reverse()
    columns = ", ".join(f"{name} integer not null" for name in names)
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, {columns}, constraint ck_counts check ({' and '.join(clauses)}))")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize(
    "column,check,expected",
    [
        ("tag TEXT", "tag is null", None),
        ("tag TEXT DEFAULT 'ready'", "tag = 'ready'", "ready"),
        ("tag TEXT DEFAULT (upper('ready'))", "tag = 'READY'", "READY"),
        ("tag INTEGER DEFAULT 7", "tag = 7", 7),
        ("tag INTEGER DEFAULT NULL", "tag is null", None),
        ("tag INTEGER", "tag = 7", None),
        ("tag TEXT GENERATED ALWAYS AS (case when n > 0 then 'ready' end)", "tag = 'ready'", "ready"),
        ("tag TEXT GENERATED ALWAYS AS (case when n > 0 then 'ready' end)", "\"tag\" = 'ready'", "ready"),
        ("tag INTEGER PRIMARY KEY", "\"tag\" > 0", 1),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_numeric_check_context_includes_omitted_columns(column, check, expected, reverse):
    columns = ["n INTEGER NOT NULL", column]
    if reverse:
        columns.reverse()
    ddl = f"create table shaped ({', '.join(columns)}, constraint ck_n check (n > 0 and {check}))"
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        objection, settled = guard.insert_seed_row(connection, "shaped", ddl, [("n", "INTEGER")], {"n": 0})
        assert (objection, settled) == ("", True)
        assert connection.execute("select n, tag from shaped").fetchall() == [(1, expected)]
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared", ["INTEGER", "NUMERIC", "BOOLEAN", "DECIMAL(10,2)", "REAL", "TEXT", "BLOB", ""])
@pytest.mark.parametrize("default", ["'abc'", "' 001 '", "'3.0e+5'", "'12x'", "x'6162'", "NULL", "1.5", "'9223372036854775808'"])
def test_numeric_context_matches_real_default_storage(declared, default):
    with sqlite3.connect(":memory:") as connection:
        connection.execute(f"create table sample(tag {declared} default {default})")
        connection.execute("insert into sample default values")
        expected = connection.execute("select tag, typeof(tag) from sample").fetchone()
        value_sql, type_sql = connection.execute("select quote(tag), quote(typeof(tag)) from sample").fetchone()
        ddl = f"create table shaped(n integer not null, tag {declared} default {default}, constraint ck check(typeof(n) = 'integer' and n > 0 and tag is {value_sql} and typeof(tag) = {type_sql}))"
        connection.execute(ddl)
        assert guard.insert_seed_row(connection, "shaped", ddl, [("n", "INTEGER")], {"n": 0}) == ("", True)
        assert connection.execute("select tag, typeof(tag) from shaped").fetchone() == expected
        assert connection.execute("select name from sqlite_temp_master").fetchall() == []


@pytest.mark.parametrize("declared", ["INTEGER", "NUMERIC", "BOOLEAN", "DECIMAL(10,2)", "REAL", "TEXT", "BLOB", "", "CHARINT", "FLOATING POINT", "STRING"])
@pytest.mark.parametrize("offered", ["abc", " 001 ", "3.0e+5", "12x", b"ab", None, 1.5, "9223372036854775808"])
def test_numeric_context_matches_real_supplied_storage(declared, offered):
    with sqlite3.connect(":memory:") as connection:
        connection.execute(f"create table sample(tag {declared})")
        connection.execute("insert into sample values (?)", (offered,))
        value_sql, type_sql = connection.execute("select quote(tag), quote(typeof(tag)) from sample").fetchone()
        expression = f"n > 0 and tag is {value_sql} and typeof(tag) = {type_sql}"
        proposals = guard.shape_proposals(connection, expression, [("n", "INTEGER"), ("tag", declared)], {"n": 0, "tag": offered}).derived
        assert ("n", 1) in proposals
        assert not any(name == "tag" for name, _ in proposals)
        assert connection.execute("select name from sqlite_temp_master").fetchall() == []


@pytest.mark.parametrize("expression", ["n > 0", "n > 0 and n < 0", "n > 0 and missing(n)"])
def test_candidate_evaluation_preserves_connection_schema_and_data(expression):
    with sqlite3.connect(":memory:") as connection:
        connection.execute("create table source(n integer)")
        connection.execute("insert into source values (37)")
        connection.execute("create temp table keep(n text)")
        connection.execute("insert into keep values ('preserved')")
        schema = connection.execute("select * from sqlite_master").fetchall()
        temp_schema = connection.execute("select * from sqlite_temp_master").fetchall()
        guard.shape_proposals(connection, expression, [("n", "INTEGER")], {"n": 0})
        assert connection.execute("select * from sqlite_master").fetchall() == schema
        assert connection.execute("select * from sqlite_temp_master").fetchall() == temp_schema
        assert connection.execute("select * from source").fetchall() == [(37,)]
        assert connection.execute("select * from keep").fetchall() == [("preserved",)]


@pytest.mark.parametrize("count", [2, 6, 16])
@pytest.mark.parametrize("reverse_columns,reverse_clauses,reverse_operands", itertools.product([False, True], repeat=3))
@pytest.mark.parametrize("comparison", ["=", "==", ">=", "<", "!="])
def test_predicate_ranked_numeric_domains_seed_nonuniform_rows(tmp_path, count, reverse_columns, reverse_clauses, reverse_operands, comparison):
    names = [f"n{index}" for index in range(1, count + 1)]
    clauses = [f"{index} {comparison} {name}" if reverse_operands else f"{name} {comparison} {index}" for index, name in enumerate(names, 1)]
    if reverse_columns:
        names.reverse()
    if reverse_clauses:
        clauses.reverse()
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, {', '.join(name + ' integer not null' for name in names)}, constraint ck check ({' and '.join(clauses)}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared,storage", [("INTEGER", "integer"), ("NUMERIC", "integer"), ("BOOLEAN", "integer"), ("DECIMAL(10,2)", "integer"), ("FLOATING POINT", "integer"), ("REAL", "real"), ("DOUBLE PRECISION", "real"), ("STRING", "integer")])
def test_numeric_repair_uses_sqlite_affinity_classification(tmp_path, declared, storage):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(n {declared} not null, constraint ck check(typeof(n)='{storage}' and n > 0))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared", ["", "DATE", "DECIMAL", "NUMERIC", "BOOLEAN", "INTEGER", "REAL", "BLOB", "TEXT"])
@pytest.mark.parametrize("shape", ["length(value)=4", "value glob 'A???'", "length(value)=4 and value glob 'A*'", "length(value)=4 and value not glob '*[0-9]*'"])
def test_text_shapes_are_not_restricted_by_initial_storage_class(tmp_path, declared, shape):
    db_path = tmp_path / "flexible.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(value {declared} not null, constraint ck check({shape}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("declared", ["NUMERIC", "REAL", "INTEGER"])
@pytest.mark.parametrize("count", [15, 20])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("repair", ["state='ready'", "json_valid(state) and json_extract(state,'$.ready')=1"])
def test_unvalidated_numeric_fallback_cannot_starve_other_repairs(tmp_path, declared, count, reverse, repair):
    columns = [f'n{index} {declared} not null' for index in range(count)]
    if reverse:
        columns.reverse()
    checks = ' and '.join([*(f'n{index}>=0' for index in range(count)), 'g=0', repair])
    db_path = tmp_path / "fallback.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped({','.join(columns)},state text not null,g integer generated always as (0),constraint ck check({checks}))")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("function", ["changes()", "total_changes()", "last_insert_rowid()"])
def test_context_sensitive_check_uses_the_real_insert_state(function):
    with sqlite3.connect(":memory:") as connection:
        ddl = f"create table shaped(n integer not null,constraint ck check(n>0 and n>{function}))"
        connection.execute(ddl)
        assert guard.insert_seed_row(connection, "shaped", ddl, [("n", "INTEGER")], {"n": 0}) == ("", True)
        assert connection.execute("select n from shaped").fetchall() == [(1,)]


@pytest.mark.parametrize("expression", ["n>0", "n>0 and n<0", "n>changes()", "n>0 and unregistered(n)"])
def test_candidate_generation_does_not_mutate_fixture_connection_state(expression):
    with sqlite3.connect(":memory:") as connection:
        connection.execute("create table held(n integer primary key)")
        connection.execute("insert into held values (17)")
        connection.execute("insert into held values (19)")
        snapshot = "select changes(),total_changes(),last_insert_rowid()"
        before = connection.execute(snapshot).fetchone()
        guard.shape_proposals(connection, expression, [("n", "INTEGER")], {"n": 0})
        assert connection.execute(snapshot).fetchone() == before
        assert connection.execute("select n from held").fetchall() == [(17,), (19,)]


@pytest.mark.parametrize("expression", ["n>0", "n>0 and n<0", "n>changes()", "n>0 and unregistered(n)"])
def test_isolated_candidate_connections_are_always_closed(monkeypatch, expression):
    connections = []
    connect = sqlite3.connect
    def tracked_connect(*args, **kwargs):
        candidate = connect(*args, **kwargs)
        connections.append(candidate)
        return candidate
    with connect(":memory:") as connection:
        monkeypatch.setattr(guard.sqlite3, "connect", tracked_connect)
        guard.shape_proposals(connection, f"n>0 and ({expression})", [("n", "INTEGER")], {"n": 0})
        assert len(connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("select 1")


@pytest.mark.parametrize("function", ["changes()", "total_changes()", "last_insert_rowid()", "local_context()"])
def test_context_functions_in_defaults_defer_to_target_insert(function):
    with sqlite3.connect(":memory:") as connection:
        connection.create_function("local_context", 0, lambda: 0)
        ddl = f"create table shaped(n integer not null,tag integer default ({function}),constraint ck check(n>0 and tag=0))"
        connection.execute(ddl)
        assert guard.insert_seed_row(connection, "shaped", ddl, [("n", "INTEGER")], {"n": 0}) == ("", True)
        assert connection.execute("select n,tag from shaped").fetchall() == [(1,0)]


@pytest.mark.parametrize("equality", ["=", "==", "IS"])
@pytest.mark.parametrize("reverse_operands,reverse_checks", itertools.product([False, True], repeat=2))
@pytest.mark.parametrize("source", ["a glob 'R-[A-C][0-9]'", "a in ('R-A0')"])
@pytest.mark.parametrize("declared", ["TEXT", "DATE", "", "BLOB"])
def test_substring_dependencies_use_normalized_equality(tmp_path, equality, reverse_operands, reverse_checks, source, declared):
    left, right = ('substr("C", 1, 4)', '"A"')
    if reverse_operands:
        left, right = right, left
    checks = [f"constraint ck_a check({source})", f"constraint ck_c check(length(c)=9 and {left} {equality} {right})"]
    if reverse_checks:
        checks.reverse()
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(id integer primary key, a {declared} not null, c {declared} not null, {', '.join(checks)})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("separate", [False, True])
def test_positive_and_negative_globs_share_one_witness(tmp_path, order, separate):
    requirements = ["length(code)=2", "code glob '[A-Z][0-9]'", "code not glob '[A-Z]0'"]
    expressions = [requirements[index] for index in order]
    if not separate:
        expressions = [" and ".join(expressions)]
    checks = ", ".join(f"constraint ck_{index} check({expression})" for index, expression in enumerate(expressions))
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped(id integer primary key, code text not null, {checks})")
    short, _ = guard.seed_representative_rows(db_path)
    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize("reverse", [False, True])
def test_prefix_sources_can_be_derived_from_json_checks(tmp_path, reverse):
    db_path = tmp_path / "vibe.sqlite"
    checks = [
        "constraint ck_source check (json_valid(a) = 1 and json_extract(a, '$.version') = 1)",
        "constraint ck_prefix check (length(b) = 20 and substr(b, 1, 13) = a)",
    ]
    if reverse:
        checks.reverse()
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table shaped (id integer primary key, a text not null, b text not null, {', '.join(checks)})")

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@pytest.mark.parametrize(
    "comparison",
    ["> 9223372036854775807", "< -9223372036854775808", "> " + "9" * 5000],
    ids=["above-int64", "below-int64", "arbitrary-precision-literal"],
)
def test_unbindable_numeric_boundaries_are_reported_not_raised(tmp_path, comparison):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"create table extreme (id integer primary key, n integer not null, constraint ck_n check (n {comparison}))")

    short, _ = guard.seed_representative_rows(db_path)

    assert "ck_n" in short["extreme"]


@pytest.mark.parametrize("value", [-(2**63), 2**63 - 1])
def test_integer_variations_remain_sqlite_bindable(value):
    with sqlite3.connect(":memory:") as connection:
        variants = [guard.varied(value, step) for step in range(1, guard.SEED_ATTEMPTS + 1)]
        assert len(set(variants)) == len(variants)
        assert value not in variants
        for varied in variants:
            assert connection.execute("select ?", (varied,)).fetchone()[0] == varied


def test_literal_and_json_repairs_use_sqlite_identifier_case_rules(tmp_path):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("""
            create table named (id integer primary key, Kind text not null, Payload text not null,
                constraint ck_kind check (kIND in ('ready')),
                constraint ck_payload check (json_valid(PAYLOAD) = 1 and json_extract(payload, '$.CaseSensitive') = 1))
        """)

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from named where Kind = 'ready' and json_extract(Payload, '$.CaseSensitive') = 1").fetchone()[0] >= guard.SEED_ROWS


def test_identifier_folding_matches_sqlite_without_changing_unicode():
    with sqlite3.connect(":memory:") as connection:
        connection.execute('create table names ("A" text, "\u00c4" text, "\u00e4" text)')
        connection.execute('insert into names values (\'ascii\', \'upper\', \'lower\')')
        assert connection.execute('select a, "\u00c4", "\u00e4" from names').fetchone() == ("ascii", "upper", "lower")
    assert guard.identifier_key("A") == guard.identifier_key("a")
    assert guard.identifier_key("\u00c4") != guard.identifier_key("\u00e4")


@pytest.mark.parametrize("width", [str(guard.SEED_TEXT_LIMIT + 1), "9" * 5000], ids=["width-limit", "huge-width"])
def test_unsupported_shapes_remain_a_visible_seed_failure(tmp_path, width):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"""
            create table oversized (id integer primary key, value text not null,
                constraint ck_width check (length(value) = {width}))
        """)

    short, _ = guard.seed_representative_rows(db_path)

    assert "ck_width" in short["oversized"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from oversized").fetchone()[0] == 0


def test_bounded_numeric_tokens_ignore_leading_zeroes():
    assert guard.bounded_integer("0" * 5000 + "12", 0, 4096) == 12
    assert guard.bounded_integer("-" + "0" * 5000 + "12", -4096, 0) == -12
    with sqlite3.connect(":memory:") as connection:
        assert guard.shape_proposals(connection, "substr(a, " + "9" * 5000 + ", 1) = b", [("a", "TEXT"), ("b", "TEXT")], {"a": "x", "b": "y"}).derived == []


@pytest.mark.parametrize("literal", ["9223372036854775808", "0001", "1.0", "-0001.5"])
def test_json_numeric_candidates_are_parsed_by_sqlite_without_integer_binding(tmp_path, literal):
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(f"""
            create table shaped (id integer primary key, payload text not null,
                constraint ck_payload check (json_valid(payload) = 1 and json_extract(payload, '$.large') = {literal}
                    and typeof(json_extract(payload, '$.large')) = typeof({literal})))
        """)

    short, _ = guard.seed_representative_rows(db_path)

    assert short == {}
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from shaped").fetchone()[0] >= guard.SEED_ROWS
        assert connection.execute("pragma integrity_check").fetchall() == [("ok",)]


@requires_release_history
def test_the_upgrade_property_runs_over_a_database_that_carries_rows(monkeypatch):
    """The seeding is only worth anything if the upgrade under test is the thing it precedes.

    Read at the moment ``run_migrations`` is called, because that is the upgrade whose
    behaviour the property is about; counting afterwards would also accept a seeder that
    ran after it, or one whose rows the upgrade had already removed.
    """
    observed: dict[str, int] = {}
    upgrade = guard.run_migrations

    def counting(db_path):
        connection = sqlite3.connect(db_path)
        try:
            observed.update(
                (str(name), connection.execute(f'select count(*) from "{name}"').fetchone()[0])
                for (name,) in connection.execute(
                    "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
                )
                if name != guard.ALEMBIC_BOOKKEEPING_TABLE
            )
        finally:
            connection.close()
        return upgrade(db_path)

    monkeypatch.setattr(guard, "run_migrations", counting)
    _, short, _ = guard.schema_gap_after_upgrade(RELEASE_HISTORY[-1])

    assert observed
    assert all(count >= guard.SEED_ROWS for table, count in observed.items() if table not in short)


@requires_release_history
def test_every_installable_tag_is_covered_exactly_once():
    """Coverage is per shipped graph: nothing skipped, nothing rebuilt.

    The unit has to be the graph rather than the tag, because a graph is what put a
    database in the field -- and because covering ~95 installable tags one database each
    would cost minutes to prove what ~30 distinct directories already prove.
    """
    covered = guard.released_graphs()

    assert {guard.versions_tree(tag) for tag in covered} == {guard.versions_tree(tag) for tag in guard.released_tags()}
    assert len({guard.versions_tree(tag) for tag in covered}) == len(covered)


@requires_release_history
def test_no_slot_is_newly_taken_twice():
    assert guard.new_slot_collisions() == {}


@requires_release_history
def test_no_released_revision_has_been_rechained():
    assert guard.rechained_revisions() == []


@requires_release_history
def test_every_released_database_still_reaches_head():
    """A database built by any released graph must reach the full schema under today's.

    This is the property the v3.0.11 outage violated, and the one no other test in this
    repository could see: every existing migration test builds its starting database by
    replaying the current chain from empty, which produces the schema the current graph
    intends rather than the schema a release actually left behind.
    """
    assert guard.unrepairable_releases()[0] == {}


@requires_release_history
@pytest.mark.parametrize("keep", [0, 1], ids=["applies-nothing", "applies-a-prefix"])
def test_a_database_the_release_never_shipped_cannot_pass(monkeypatch, keep, tmp_path):
    """The guard's own false negative, closed at the only revision that proves anything.

    An extraction that resolves to no revisions -- or to some prefix of the release --
    reports success and leaves a database that release never shipped, which then upgrades
    to today's head with a complete schema: a pass proving the opposite of what it claims.
    Every intermediate revision is a database today's graph can legitimately repair, so
    only the released graph's own head is evidence it was applied in full.
    """

    extract = guard.extract_released_versions

    def truncated(tag: str, destination: Path) -> Path:
        versions = extract(tag, destination)
        for path in sorted(versions.glob("*.py"))[keep:]:
            path.unlink()
        return versions

    monkeypatch.setattr(guard, "extract_released_versions", truncated)

    with pytest.raises(guard.MigrationGuardError):
        guard.schema_gap_after_upgrade(RELEASE_HISTORY[-1])


@requires_spliced_baseline
def test_the_guard_still_detects_the_release_it_was_written_for():
    """Run against the release before the splice, the guard must report it.

    Without this, every assertion above could pass because the guard stopped detecting
    anything, and nothing in the suite would notice.
    """
    problems = guard.rechained_revisions(SPLICED_BASELINE)

    assert [problem for problem in problems if SPLICED_REVISION in problem]
    assert guard.new_slot_collisions(SPLICED_BASELINE)
