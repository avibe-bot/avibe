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

import pytest

from scripts import migration_release_guard as guard

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


def _revision_file(revision: str, down_revision: str) -> str:
    return f'"""a migration"""\n\nrevision = "{revision}"\ndown_revision = {down_revision}\n'


# One revision of every shape the real graph contains: a root with no parent, an ordinary
# linear child, a sibling branch, and a merge whose parent is a tuple. Seeding every shape
# rather than listing the ones that must not regress means a shape introduced later is
# covered the moment it joins this table, without editing a single test.
SHIPPED_REVISIONS = (
    ("20260101_0001", "root", "None"),
    ("20260102_0002", "linear", '"20260101_0001"'),
    ("20260103_0003", "branch", '"20260101_0001"'),
    ("20260104_0004", "merge", '("20260102_0002", "20260103_0003")'),
)

SHIPPED_GRAPH = {
    f"{revision}_{slug}.py": _revision_file(revision, down_revision)
    for revision, slug, down_revision in SHIPPED_REVISIONS
}


def test_an_unchanged_graph_reports_nothing(monkeypatch):
    _graphs(monkeypatch, SHIPPED_GRAPH, dict(SHIPPED_GRAPH))

    assert guard.rechained_revisions("v0.0.0") == []
    assert guard.new_slot_collisions("v0.0.0") == {}


@pytest.mark.parametrize(("revision", "slug"), [(revision, slug) for revision, slug, _ in SHIPPED_REVISIONS])
def test_repointing_any_released_revision_is_reported(monkeypatch, revision, slug):
    current = dict(SHIPPED_GRAPH)
    current[f"{revision}_{slug}.py"] = _revision_file(revision, '"20269999_9999"')
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")
    assert len(problems) == 1
    assert revision in problems[0]


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


def test_a_computed_revision_identifier_is_not_silently_skipped(monkeypatch):
    """A revision that computes its parent cannot be compared, so it must not read as equal."""
    current = dict(SHIPPED_GRAPH)
    current["20260102_0002_linear.py"] = '"""a migration"""\n\nrevision = "20260102_0002"\ndown_revision = ROOT\n'
    _graphs(monkeypatch, SHIPPED_GRAPH, current)

    problems = guard.rechained_revisions("v0.0.0")
    assert len(problems) == 1
    assert "<computed>" in problems[0]


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
def test_a_released_graph_that_applies_nothing_cannot_pass(monkeypatch):
    """The guard's own false negative, closed.

    An Alembic run whose ``version_locations`` resolves to no revisions reports success
    and applies nothing, and the empty database it leaves behind then upgrades to today's
    head with a complete schema -- a pass that proves the opposite of what it claims.
    """
    monkeypatch.setattr(
        guard,
        "extract_released_versions",
        lambda tag, destination: (destination.mkdir(parents=True, exist_ok=True), destination)[1],
    )

    with pytest.raises(guard.MigrationGuardError):
        guard.missing_tables_after_upgrade(RELEASE_HISTORY[-1])


@requires_spliced_baseline
def test_the_guard_still_detects_the_release_it_was_written_for():
    """Run against the release before the splice, the guard must report it.

    Without this, every assertion above could pass because the guard stopped detecting
    anything, and nothing in the suite would notice.
    """
    problems = guard.rechained_revisions(SPLICED_BASELINE)

    assert [problem for problem in problems if SPLICED_REVISION in problem]
    assert guard.new_slot_collisions(SPLICED_BASELINE)
