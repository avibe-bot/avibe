"""Cover the migration release guard's detection and the properties it asserts.

Two layers. The synthetic tests pin the detection itself, so the guard cannot decay into
something that passes because it stopped looking. The repository tests are the guard
running for real: three of them state the invariant the working tree must hold, and one
runs the guard against the release that preceded the v3.0.11 splice, which is the known
positive that keeps the other three honest.

Anything reading release history needs tags, which a shallow CI checkout does not have.
Those tests skip there and the ``migration-release-guard`` workflow runs the same
properties through the CLI with the full history fetched.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest
from alembic.script.revision import Revision

from scripts import migration_release_guard as guard
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


@requires_release_history
def test_no_released_migration_body_has_been_edited():
    """The claim measured against the graph that actually ships, not a synthetic one."""
    assert guard.edited_released_bodies() == []


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
    monkeypatch.setattr(guard, "collect_problems", lambda baseline, **kwargs: ("v9.9.9", problems))

    assert guard.main([]) == expected_exit
    for problem in problems:
        assert problem in capsys.readouterr().err


def test_the_command_line_refuses_rather_than_passing_without_history(monkeypatch, capsys):
    def unreachable(*args, **kwargs):
        raise guard.MigrationGuardError("no release tag carries the migrations directory")

    monkeypatch.setattr(guard, "collect_problems", unreachable)

    assert guard.main([]) == 2
    assert "could not run" in capsys.readouterr().err


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
    assert guard.unrepairable_releases() == {}


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
