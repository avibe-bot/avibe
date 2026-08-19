#!/usr/bin/env python3
"""Hold the Alembic migration graph to the shape its releases already shipped.

A released migration graph is a shipped surface. Databases in the field were built by it,
they record only the revision they reached, and Alembic walks them forward from there and
never back. So editing a released revision's parentage does not change what those
databases have already done -- it changes what they will do next, silently, with no error
at the moment of the edit and no error at the moment of the upgrade.

Five properties, one primitive: the graph as a released tag actually shipped it, read
straight out of git. Nothing here is a hand-maintained list of known-bad cases, and
nothing needs an old Python environment -- ``env.py`` reads ``target_metadata`` only
during autogenerate, so today's runtime can drive an older ``version_locations``.

    fresh_install_tables()        HEAD_TABLES is what a fresh install actually has, so
                                  the property below derives from something measured
    new_slot_collisions()         a change introduces no new duplicated slot number
    rechained_revisions()         a released revision keeps its identity and every edge
                                  Alembic orders the graph by
    edited_released_bodies()      a released revision still does what the release ran
    unrepairable_releases()       a database built by a released graph, carrying rows,
                                  still comes out ready when the upgrade users run
                                  upgrades it

Readiness is not this module's opinion. The last property drives ``run_migrations`` and
asks ``background_tables_ready``, so it covers the repair and stamping that run before
Alembic does, and it means by "ready" exactly what production means -- required columns
included, not merely a full set of table names. It seeds the released schema first,
because an empty database is the one case no user is in and is the case under which
adding a NOT NULL column, tightening a nullable one, and building a unique index all
succeed unconditionally.

A release here is any installable tag, ``gh-vX.Y.ZrcN`` prereleases included, because a
database in the field can have been built by one. The first three properties are metadata
only; the fourth builds a database per *distinct shipped graph*, which is what makes that
wider window affordable -- many tags ship byte-identical migrations.

Run it directly, or through the ``migration-release-guard`` workflow, which is the job
that supplies the full tag history this needs.
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from alembic import command
from alembic.config import Config
from alembic.script.base import _only_source_rev_file
from alembic.util import to_tuple
from packaging.version import InvalidVersion, Version

from scripts.release_package_version import package_version_from_release_tag
from storage.migrations import (
    HEAD_ONLY_REQUIRED_COLUMNS,
    HEAD_REQUIRED_COLUMNS,
    HEAD_TABLES,
    alembic_dir,
    background_tables_ready,
    run_migrations,
)

VERSIONS_PATH = "storage/alembic/versions"

# ``20260806_0047_remove_session_queue_hold.py`` -> slot ``0047``. Two branches choosing
# the same slot is how the graph forked: the filenames differ, so git merges both without
# a conflict and the duplicate number is the only trace left.
SLOT_PATTERN = re.compile(r"^\d{8}_(\d{4})_.*\.py$")

# Alembic's own bookkeeping. It is the one table a database has that no migration
# declares, so it is the one exclusion when comparing a real database to HEAD_TABLES.
ALEMBIC_BOOKKEEPING_TABLE = "alembic_version"


class MigrationGuardError(RuntimeError):
    """Raised when the guard cannot reach the release history it exists to compare against."""


def is_migration_source(name: str) -> bool:
    """Whether Alembic will load ``name`` as a migration, decided by Alembic's own rule.

    Borrowed rather than restated, for the same reason ``GRAPH_FIELDS`` is measured off
    ``Revision.__init__``: a file Alembic imports is a file this guard must account for,
    so the two cannot be permitted to disagree about which files those are. Alembic
    rejects a matching file that declares no ``revision`` outright, so nothing this admits
    is optional for it either -- which makes it the right denominator for coverage.
    """
    return _only_source_rev_file.match(name) is not None


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise MigrationGuardError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def release_version(tag: str) -> Version | None:
    """``tag``'s package version, or ``None`` if it is not a release tag.

    Borrowed rather than restated, the same way ``is_migration_source`` is. The publish
    workflow turns a tag into a wheel by asking ``package_version_from_release_tag``, so a
    tag it accepts is a tag that shipped -- and a rule stated separately here could only
    ever come to disagree with the one that actually decides. Disagreeing in the direction
    that drops tags is the expensive one: a release the filter does not recognise is
    absent from every property below, so a migration first shipped in it can be rechained
    or edited afterwards with nothing to compare against.

    ``gh-vX.Y.ZrcN`` prereleases are installable -- AGENTS.md §9 requires a wheel and an
    sdist in their release assets -- so a database in the field can have been built by
    one, and PEP 440 already orders them before the release they lead to.
    """
    try:
        return Version(package_version_from_release_tag(tag))
    except (ValueError, InvalidVersion):
        return None


def version_key(tag: str) -> tuple[Version, str]:
    """Sort key ordering release tags by PEP 440, with the tag itself breaking ties.

    ``vX.Y.Z`` and ``gh-vX.Y.Z`` carry one version between them, so something has to make
    the order total; the tag string does it, and puts ``v`` last, which is what makes the
    newest baseline the real release rather than its prerelease.
    """
    version = release_version(tag)
    if version is None:
        raise MigrationGuardError(f"{tag!r} is not a release tag")
    return (version, tag)


def versions_tree(tag: str) -> str | None:
    """The git object id of ``tag``'s versions directory, or ``None`` if it shipped none.

    Identity of a shipped graph, straight from git: two tags with the same object id
    shipped byte-identical migrations and therefore put the same database in the field.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{tag}^{{}}:{VERSIONS_PATH}"],
        capture_output=True,
        check=False,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip() or None


def released_tags() -> list[str]:
    """Every release tag that shipped a migrations directory, oldest first.

    The window is derived, not chosen. A tag without ``storage/alembic/versions`` never
    put a migrated database in the field; every tag with one did. A "last N releases"
    cutoff would instead stop covering a release on a schedule unrelated to whether
    anyone is still running it.
    """
    names = _git("tag", "-l", "v*", "-l", "gh-v*").split()
    candidates = sorted((tag for tag in names if release_version(tag)), key=version_key)
    tags = [tag for tag in candidates if versions_tree(tag)]
    if not tags:
        # A guard that reads "no baseline" as "nothing to compare, therefore fine" passes
        # forever while proving nothing. Refuse instead, and name the cause: a shallow
        # checkout is the way this happens in practice.
        raise MigrationGuardError(
            f"no release tag carries {VERSIONS_PATH}, so there is no shipped graph to compare "
            "against; a shallow checkout without tags produces exactly this (CI needs "
            "actions/checkout with fetch-depth: 0)"
        )
    return tags


def latest_released_tag() -> str:
    """The baseline for the metadata properties: the newest release carrying migrations.

    Comparing against the newest release rather than against all of them is what keeps
    the guard honest about the past. A splice that has already shipped cannot be
    un-shipped -- reverting it would break the databases that did apply it -- so demanding
    that history be clean would leave the guard permanently red and therefore ignored.
    Each release is instead checked against its predecessor while it is still unreleased,
    and the invariant chains forward without relitigating anything.
    """
    return released_tags()[-1]


def released_graphs() -> list[str]:
    """One tag per distinct shipped graph, oldest first: the first release that shipped it.

    The unit is a graph, not a tag. What puts a database in the field is a versions
    directory, so the many prerelease tags that ship a byte-identical one all put the same
    database there and building it once is building it for all of them. That is what makes
    covering every installable tag affordable: one ``git rev-parse`` per tag instead of one
    database per tag, with no release left unexercised.
    """
    graphs: dict[str, str] = {}
    for tag in released_tags():
        graphs.setdefault(str(versions_tree(tag)), tag)
    return sorted(graphs.values(), key=version_key)


def released_sources(tag: str) -> dict[str, str]:
    """``{filename: source}`` for the migration files exactly as ``tag`` shipped them."""
    names = _git("ls-tree", "--name-only", f"{tag}:{VERSIONS_PATH}").split()
    return {name: _git("show", f"{tag}:{VERSIONS_PATH}/{name}") for name in names if name.endswith(".py")}


def working_tree_sources() -> dict[str, str]:
    """``{filename: source}`` for the migration files as they are right now."""
    return {path.name: path.read_text(encoding="utf-8") for path in (REPO_ROOT / VERSIONS_PATH).glob("*.py")}


def extract_released_versions(tag: str, destination: Path) -> Path:
    """Materialise ``tag``'s versions directory under ``destination`` for Alembic to walk."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", tag, VERSIONS_PATH],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive, check=True)
    return destination / VERSIONS_PATH


def slot_collisions(filenames: Iterable[str]) -> dict[str, set[str]]:
    """``{slot: filenames}`` for every slot number claimed by more than one file."""
    slots: dict[str, set[str]] = defaultdict(set)
    for name in filenames:
        match = SLOT_PATTERN.match(name)
        if match:
            slots[match.group(1)].add(name)
    return {slot: names for slot, names in slots.items() if len(names) > 1}


def new_slot_collisions(baseline: str | None = None) -> dict[str, set[str]]:
    """Slots with a claimant the baseline release did not already ship.

    The collisions history already contains are in the baseline itself, so they need no
    allowlist -- and cannot decay into one that outlives the reason it was written. What
    the baseline excuses is those exact filenames, though, not the slot number: excusing
    the number would let a third claimant join an already-duplicated slot unreported,
    which is the same fork the guard exists to catch.
    """
    baseline = baseline or latest_released_tag()
    shipped = slot_collisions(released_sources(baseline))
    return {
        slot: names
        for slot, names in slot_collisions(working_tree_sources()).items()
        if names - shipped.get(slot, set())
    }


class _ComputedMetadata:
    """A ``revision`` or ``down_revision`` the guard could not read as a literal.

    Never equal to anything, including another instance of itself. Two computed
    expressions are not evidence that a graph is unchanged, they are the absence of
    evidence, and a sentinel that compared equal would turn "cannot verify" into
    "verified unchanged" -- silently, and precisely for the revisions least able to
    afford it.
    """

    __slots__ = ()
    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return "<computed>"

    def __eq__(self, other: object) -> bool:
        return False


COMPUTED = _ComputedMetadata()


# The module-level names Alembic itself reads out of a migration, mapped to the
# ``Revision`` constructor arguments they become. Comparing fewer than all of them
# compares a graph the guard invented rather than the one Alembic builds, and the
# difference is a hole of exactly the outage's shape: the ordering changes while every
# comparison here still matches. The mapping is held to that signature by a test, so a
# field added upstream fails loudly instead of going unwatched.
GRAPH_FIELDS = {
    "revision": "revision",
    "down_revision": "down_revision",
    "depends_on": "dependencies",
    "branch_labels": "branch_labels",
}

GRAPH_EDGES = tuple(field for field in GRAPH_FIELDS if field != "revision")


def declared_graph_fields(source: str) -> dict[str, object]:
    """The graph declarations a migration module makes, in either form Python allows.

    ``revision: str = "..."`` is an ``AnnAssign``, and reading only ``Assign`` does not
    merely lose the annotation -- it drops the migration out of the graph entirely, so it
    is compared against nothing and any rechain of it passes.

    Edge values are normalized with Alembic's own ``to_tuple``, exactly as ``Script``
    does, so the guard's notion of "the same parent" is Alembic's notion and re-spelling
    a value it already treats as equal is not reported as drift.
    """
    declared: dict[str, object] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name) or target.id not in GRAPH_FIELDS:
            continue
        try:
            literal = ast.literal_eval(value)
        except ValueError:
            declared[target.id] = COMPUTED
            continue
        declared[target.id] = literal if target.id == "revision" else to_tuple(literal, default=())
    return declared


def revision_claims(sources: dict[str, str]) -> dict[str, list[tuple[str, dict[str, object]]]]:
    """Every claim on every revision identifier, contested ones included.

    Parsing keeps this free of import side effects and lets it read a revision from a
    release whose modules would no longer import against today's code. A computed value
    becomes ``COMPUTED``, which compares unequal to everything; a computed *revision*
    additionally cannot identify its file, so it is keyed by filename and can never be
    mistaken for a literal revision that happens to render the same way. An edge a module
    never declares is ``()``, which is what Alembic defaults it to.

    The identifier is the guard's only handle on "the same migration across two releases",
    and two modules can declare it at once. Keeping the claimants as a list is what lets a
    caller report that rather than overwrite one of them: the survivor would otherwise
    answer for a migration whose body Alembic will skip on any database stamped with the
    shared identifier.
    """
    claims: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for name, source in sorted(sources.items()):
        declared = declared_graph_fields(source)
        if "revision" not in declared:
            continue
        revision = declared["revision"]
        key = f"<computed:{name}>" if revision is COMPUTED else str(revision)
        edges = {field: declared.get(field, ()) for field in GRAPH_EDGES}
        claims.setdefault(key, []).append((name, edges))
    return claims


def revision_graph(sources: dict[str, str]) -> dict[str, tuple[str, dict[str, object]]]:
    """``{revision: (filename, {edge_field: value})}`` for identifiers naming one migration.

    Only a contested identifier is absent, because it cannot be keyed to one migration at
    all; ``ungraphable_sources`` reports it instead. Metadata that merely cannot be read
    stays, carrying ``COMPUTED`` values that are unequal to everything -- a comparison
    involving one reports drift rather than agreement, which is the honest answer where
    nothing can be read. Dropping it here instead would be silently destructive on a
    released tree, whose files are the only surviving record of what that release declared.
    """
    return {
        revision: claims[0] for revision, claims in revision_claims(sources).items() if len(claims) == 1
    }


def ungraphable_sources(sources: dict[str, str]) -> dict[str, str]:
    """``{filename: why the guard cannot hold that file to a released graph}``.

    One defect wearing three faces: a key the guard invents has stopped naming exactly one
    migration. Metadata it cannot read as a literal names nothing, an identifier two
    modules declare names two things, and a file declaring no revision at all is never
    keyed and so was never named by anything. Either way the honest answer is "cannot
    verify", and the one answer that must stay unreachable is "verified unchanged".

    That third face is why the denominator below is Alembic's file rule and not this
    module's parse of those files. Asking the parser which files there were to read is
    self-confirming -- a file it cannot see is absent from its own account of what it saw
    -- and each earlier version of this function asked exactly that, which is why the same
    defect kept arriving wearing a new face.

    Every caller reading a source tree owes this its output: a tree is compared only
    alongside the reasons parts of it cannot be, or refused outright.
    """
    reasons: dict[str, str] = {}
    claims = revision_claims(sources)
    for revision, claimants in claims.items():
        names = [name for name, _ in claimants]
        for name, edges in claimants:
            if revision.startswith("<computed:") or any(value is COMPUTED for value in edges.values()):
                reasons[name] = "computes its migration metadata"
            elif len(claimants) > 1:
                others = ", ".join(other for other in names if other != name)
                reasons[name] = f"declares revision {revision!r}, which {others} also declares"

    keyed = {name for claimants in claims.values() for name, _ in claimants}
    for name in sources:
        if name not in keyed and is_migration_source(name):
            reasons[name] = "declares no module-level revision"
    return reasons


def shipped_head_revisions(sources: dict[str, str]) -> set[str]:
    """The revisions nothing in ``sources`` descends from: where a full upgrade of them ends.

    ``depends_on`` is an ordering constraint rather than a parent link, so only
    ``down_revision`` decides what is still a head -- the same rule Alembic applies.

    Unreadable metadata is refused rather than worked around, because a head is one answer
    and there is no partial version of it. A parent nobody can resolve leaves the real
    ancestor looking like a head, so an upgrade that stopped early there would satisfy the
    assertion this exists to make.
    """
    unreadable = ungraphable_sources(sources)
    if unreadable:
        raise MigrationGuardError(
            "cannot locate the head of a graph whose metadata is unreadable: "
            + "; ".join(f"{name} {reason}" for name, reason in sorted(unreadable.items()))
        )
    graph = revision_graph(sources)
    parents = {parent for _, edges in graph.values() for parent in edges["down_revision"]}
    return set(graph) - parents


def rechained_revisions(baseline: str | None = None) -> list[str]:
    """Released revisions whose identity or graph edges have changed since the baseline.

    This is the property the outage broke. Inserting an ancestor behind a revision the
    field has already passed is invisible to Alembic, which only walks forward, and
    invisible to a single-head assertion, which a merge revision satisfies. It is visible
    here because the shipped parent is read from the release rather than inferred from
    the graph that replaced it.
    """
    baseline = baseline or latest_released_tag()
    working_tree = working_tree_sources()
    shipped_tree = released_sources(baseline)
    claimed = revision_claims(working_tree)
    current = revision_graph(working_tree)
    # Both trees, because a comparison is only as trustworthy as its less readable side.
    # A released declaration nobody can read is not one to pass over quietly: those files
    # are the only surviving record of what the release shipped, so losing one loses the
    # thing every later revision would have been held to.
    unreadable_now = ungraphable_sources(working_tree)
    unreadable_then = ungraphable_sources(shipped_tree)
    problems = [
        f"{name} {reason}, so the guard cannot hold it to {baseline}"
        for name, reason in sorted(unreadable_now.items())
    ]
    problems += [
        f"{baseline} shipped {name}, which {reason}, so nothing can be held to what it declared"
        for name, reason in sorted(unreadable_then.items())
        if name not in unreadable_now
    ]
    for revision, (name, shipped) in sorted(revision_graph(shipped_tree).items()):
        if name in unreadable_then:
            # The baseline's own declaration is what cannot be read, so there is nothing
            # to compare against and the reason above is the whole report.
            continue
        if revision not in current:
            if revision in claimed:
                # Still claimed, just not readably: every claimant is named once in the
                # reasons above, and repeating it here describes one defect as two.
                continue
            problems.append(f"{revision} ({name}) shipped in {baseline} and is no longer in the graph")
            continue
        if current[revision][0] in unreadable_now:
            continue
        edges = current[revision][1]
        drift = "; ".join(
            f"{field} {value!r} -> {edges[field]!r}" for field, value in shipped.items() if edges[field] != value
        )
        if drift:
            problems.append(f"{revision} ({name}) no longer matches what {baseline} shipped: {drift}")
    return problems


def released_bodies(sources: dict[str, str]) -> dict[str, tuple[str, str]]:
    """``{revision: (filename, source)}`` -- what each migration in ``sources`` runs.

    Keyed by revision because that is the key Alembic uses. A filename is not a migration's
    identity: databases record the revision, ``version_locations`` is scanned rather than
    enumerated, and renaming a file changes nothing Alembic can observe. Keying bodies by
    filename would therefore let a rename carry an edit across a release boundary
    untouched, which is the same mistake as inventing a key for anything else here.
    """
    return {
        revision: (name, sources[name])
        for revision, (name, _) in revision_graph(sources).items()
        if is_migration_source(name)
    }


def edited_released_bodies(baseline: str | None = None) -> list[str]:
    """Released revisions whose migration no longer does what the baseline ran.

    The properties above watch what a migration *declares*; this watches what it *does*.
    They are one contract seen from two sides -- a released migration is a shipped surface
    -- and this repository has produced one defect of each shape: the v3.0.11 outage moved
    a ``down_revision``, and ``20260501_0001`` had its ``upgrade()`` body edited after
    release. Editing the body is the quieter half. A database stamped at that revision
    never reruns it, a fresh install runs only the new version, and nothing raises at
    either moment.

    The upgrade property notices such an edit only when the divergence reaches readiness.
    An added index, constraint, or backfill leaves both databases ready and permanently
    different, so it cannot be relied on to notice in general -- and concluding otherwise
    from the one edit it did catch is how this gap survived a whole design pass.

    A released revision missing from the working tree is passed over here because
    ``rechained_revisions`` is what reports it, and from the same two calls: either its
    file is unreadable or contested, or the revision is gone from the graph outright.
    Restating it would describe one defect as two. The pair is what must be exhaustive,
    not either half, and ``collect_problems`` runs both.

    No exemption for edits judged harmless. Judging them is the convention this guard
    exists to replace, and no exemption is needed: restoring the shipped file and adding a
    new migration carrying the change is always available, and is the only form of the
    change every database can actually apply.
    """
    baseline = baseline or latest_released_tag()
    current = released_bodies(working_tree_sources())
    problems = []
    for revision, (shipped_name, source) in sorted(released_bodies(released_sources(baseline)).items()):
        if revision not in current or current[revision][1] == source:
            continue
        name = current[revision][0]
        where = shipped_name if name == shipped_name else f"{shipped_name}, now {name}"
        problems.append(
            f"{revision} ({where}) is no longer what {baseline} shipped; a released "
            "migration's body is fixed once a database has run it"
        )
    return problems


def _alembic_config(db_path: Path, versions: Path | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(alembic_dir()))
    if versions is not None:
        # Without this Alembic splits version_locations on spaces and commas, so a
        # temporary directory containing either would silently resolve to no location at
        # all -- and an empty graph upgrades to head without applying anything.
        config.set_main_option("path_separator", "os")
        config.set_main_option("version_locations", str(versions))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


def table_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        return {str(row[0]) for row in connection.execute("select name from sqlite_master where type = 'table'")}
    finally:
        connection.close()


def fresh_install_tables() -> set[str]:
    """The tables a fresh install actually has, built the way production builds one."""
    with tempfile.TemporaryDirectory() as workspace:
        db_path = Path(workspace) / "vibe.sqlite"
        run_migrations(db_path)
        return table_names(db_path) - {ALEMBIC_BOOKKEEPING_TABLE}


def stamped_revision(db_path: Path) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"select version_num from {ALEMBIC_BOOKKEEPING_TABLE}").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return str(rows[0][0]) if len(rows) == 1 else None


def describe_schema_gap(db_path: Path) -> str:
    """Why ``background_tables_ready`` rejected this database, in one line.

    The verdict is production's; this only reads the same tables and columns back to say
    what is missing, because "not ready" alone does not tell anyone what to fix.
    """
    tables = table_names(db_path)
    missing_tables = HEAD_TABLES - tables
    missing_columns = []
    connection = sqlite3.connect(db_path)
    try:
        for table, required in sorted((HEAD_REQUIRED_COLUMNS | HEAD_ONLY_REQUIRED_COLUMNS).items()):
            if table in missing_tables or table not in tables:
                continue
            present = {str(row[1]) for row in connection.execute(f'pragma table_info("{table}")')}
            missing_columns.extend(f"{table}.{column}" for column in sorted(required - present))
    finally:
        connection.close()

    parts = []
    if missing_tables:
        parts.append(f"{len(missing_tables)} table(s) missing: {', '.join(sorted(missing_tables))}")
    if missing_columns:
        parts.append(f"{len(missing_columns)} column(s) missing: {', '.join(missing_columns)}")
    return "; ".join(parts) or "the schema is incomplete in a way HEAD_TABLES and the required columns do not name"


# SQLite names the one constraint an insert tripped, which is what makes the repair below
# a guided search rather than a guess: the database says what it objected to, and the
# objection itself carries the values it would have accepted.
CHECK_FAILURE = re.compile(r"CHECK constraint failed: (\w+)")

# Enough attempts to satisfy every constraint a row trips one at a time, and few enough
# that a table whose constraints cannot be satisfied this way is given up on rather than
# searched for. Reached only on the tables that refuse the first row at all.
SEED_ATTEMPTS = 60


def representative_value(column: str, declared_type: str) -> object:
    """A value of ``declared_type`` that SQLite will store in ``column``.

    Typed off the declaration rather than off the column's meaning, because meaning is
    what no generic seeder can have and what a per-release fixture would have to restate
    once per shipped graph. The name is consulted only where the declared type leaves the
    choice open -- every TEXT column would otherwise get the same opaque string -- so a
    migration that parses a timestamp, an address, or a JSON document gets something
    parseable rather than ``'x'``.
    """
    kind = declared_type.upper()
    if any(marker in kind for marker in ("INT", "REAL", "NUM", "DOUB", "FLOA", "BOOL")):
        return 0
    if "BLOB" in kind:
        return b""
    if column.endswith(("_at", "_time")):
        return "1970-01-01T00:00:00+00:00"
    if column.endswith("_json"):
        return "{}"
    if "email" in column:
        return "seed@example.invalid"
    return "x"


def check_expression(ddl: str, name: str) -> str:
    """Constraint ``name``'s expression as ``ddl`` declares it, or ``''`` if it has none."""
    # SQLite stores the DDL as it was written, so the keywords are whatever case the
    # migration that created the table happened to use.
    marker = re.search(rf'CONSTRAINT\s+"?{re.escape(name)}"?\s+CHECK\s*\(', ddl, re.IGNORECASE)
    if marker is None:
        return ""
    depth = 1
    index = marker.end()
    for offset in range(index, len(ddl)):
        if ddl[offset] == "(":
            depth += 1
        elif ddl[offset] == ")":
            depth -= 1
            if depth == 0:
                return ddl[index:offset]
    return ""


def check_proposals(expression: str, columns: Iterable[str]) -> list[tuple[str, str]]:
    """``(column, value)`` pairs ``expression`` itself suggests, for the columns it names.

    A check the row tripped is a check that says what it wants: ``state in ('waiting',
    ...)`` carries both the column to change and the values it would have accepted. Only
    columns the expression names are proposed for, because a literal from one constraint
    written into a column that constraint never mentions is not a repair.

    Reading the expression is a heuristic and is allowed to be wrong. SQLite decides
    whether the resulting row is admissible, so a bad proposal costs an attempt rather
    than producing a row the schema would have refused.
    """
    literals = re.findall(r"'([^']*)'", expression)
    named = [column for column in columns if re.search(rf"\b{re.escape(column)}\b", expression)]
    return [(column, literal) for column in named for literal in literals]


def seed_representative_rows(db_path: Path) -> set[str]:
    """Put one row in every table of the schema ``db_path`` holds; return the ones that refused.

    A migration is only as safe as the data it runs over. On an empty database, adding a
    NOT NULL column with no default, tightening a nullable one, backfilling from a column
    being dropped, and building a unique index all succeed unconditionally -- so an
    upgrade proved on an empty database is proved against the one case no user is in.
    Seeding first is what makes the property below about the databases users carry.

    The rows are derived from the schema, because the schema is what differs in every
    release and ``pragma table_info`` already knows it; a fixture written per release
    would cover the graphs that existed when it was written and quietly stop at the next
    one. Only what the schema requires is filled -- NOT NULL or primary key, no default.
    Leaving the rest NULL is deliberate and the more adversarial choice: a migration that
    later makes a nullable column NOT NULL has to survive the NULLs a real database has.

    Where a CHECK rejects that row, SQLite names the constraint and the constraint names
    its own accepted values, so the repair proposes those and asks again. SQLite is the
    judge throughout: a table is seeded only if the schema accepted the row.

    What this proves is that the upgrade runs over rows, not that it runs over meaningful
    ones -- foreign keys are off, which is SQLite's default and is what lets each table be
    seeded on its own, so the rows are individually well-formed and collectively
    inconsistent. Tables whose constraints encode a shape only the writing code knows are
    returned rather than skipped quietly, so the coverage this leaves out is a number the
    caller can print instead of an assumption.
    """
    refused = set()
    with sqlite3.connect(db_path) as connection:
        schema = [
            (str(name), str(ddl))
            for name, ddl in connection.execute(
                "select name, sql from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            )
            if name != ALEMBIC_BOOKKEEPING_TABLE
        ]
        for table, ddl in schema:
            required = [
                (str(row[1]), str(row[2]))
                for row in connection.execute(f'pragma table_info("{table}")')
                if (row[3] or row[5]) and row[4] is None
            ]
            if not required:
                connection.execute(f'insert into "{table}" default values')
                continue
            columns = ", ".join(f'"{column}"' for column, _ in required)
            placeholders = ", ".join("?" for _ in required)
            statement = f'insert into "{table}" ({columns}) values ({placeholders})'
            values = {column: representative_value(column, declared) for column, declared in required}
            proposed: set[tuple[str, str]] = set()
            for _ in range(SEED_ATTEMPTS):
                try:
                    connection.execute(statement, [values[column] for column, _ in required])
                    break
                except sqlite3.Error as exc:
                    # However SQLite refuses the row, the table is simply one this cannot
                    # seed. Only a named CHECK carries a repair, so anything else ends here.
                    failure = CHECK_FAILURE.search(str(exc))
                    if failure is None:
                        break
                    expression = check_expression(ddl, failure.group(1))
                    untried = [
                        pair
                        for pair in check_proposals(expression, [column for column, _ in required])
                        if pair not in proposed
                    ]
                    if not untried:
                        break
                    proposed.add(untried[0])
                    values[untried[0][0]] = untried[0][1]
            if connection.execute(f'select count(*) from "{table}"').fetchone()[0] == 0:
                refused.add(table)
        connection.commit()
    return refused


def schema_gap_after_upgrade(tag: str) -> tuple[str | None, set[str]]:
    """``(gap, unseeded)`` -- why a database built by ``tag``'s graph is not ready, and what carried no row.

    ``gap`` is ``None`` when the database comes out ready. ``unseeded`` is the tables the
    upgrade therefore ran over empty, reported rather than dropped so the coverage this
    property does not have is a number someone can read.

    The second stage runs ``storage.migrations.run_migrations`` rather than Alembic
    directly, and the verdict comes from ``background_tables_ready``. Both are deliberate:
    the property is about the databases users actually carry through the upgrade users
    actually run, so a regression in the pre-Alembic repair or stamping path has to fail
    here too, and readiness has to mean what production means by it -- required columns
    included, not merely a full set of table names.

    Whatever the upgrade raises when it aborts propagates: a crash and a silently
    incomplete schema are the same failure of the same property.
    """
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        db_path = root / "vibe.sqlite"
        released = extract_released_versions(tag, root / "released")
        # Read from the tag's own files, before Alembic is involved at all. Asking the
        # resolved graph for its head instead would make this self-confirming: an
        # extraction or ``version_locations`` that silently resolved to a prefix produces a
        # smaller graph whose head the run then trivially reaches, and it is exactly that
        # database -- one the release never put in the field, and one today's graph can
        # legitimately repair -- whose clean upgrade would be read as evidence.
        shipped_heads = shipped_head_revisions(released_sources(tag))
        command.upgrade(_alembic_config(db_path, released), "head")

        stamp = stamped_revision(db_path)
        if stamp is None or stamp not in shipped_heads:
            raise MigrationGuardError(
                f"upgrading with {tag}'s graph left the database stamped {stamp!r} rather than at "
                f"its head {sorted(shipped_heads)!r}; the released graph was not applied in full"
            )

        unseeded = seed_representative_rows(db_path)
        run_migrations(db_path)
        gap = None if background_tables_ready(db_path) else describe_schema_gap(db_path)
        return gap, unseeded


def unrepairable_releases() -> tuple[dict[str, str], set[str]]:
    """``({tag: reason}, unseeded)`` for every release today's graph cannot bring to head.

    ``unseeded`` is the union across releases: a table no shipped schema would accept a
    generic row for, and therefore one no release proved its upgrade over rows of.
    """
    failures = {}
    unseeded: set[str] = set()
    for tag in released_graphs():
        try:
            gap, refused = schema_gap_after_upgrade(tag)
        except Exception as exc:  # noqa: BLE001 - however the upgrade fails, the verdict is the same
            failures[tag] = f"upgrade aborted with {type(exc).__name__}: {str(exc).splitlines()[0]}"
        else:
            unseeded |= refused
            if gap is not None:
                failures[tag] = f"upgrade left the database not ready: {gap}"
    return failures, unseeded


def collect_problems(
    baseline: str | None = None, *, include_upgrade: bool = True
) -> tuple[str, list[str], list[str]]:
    """``(baseline, problems, notes)`` -- every property checked, every violation reported at once.

    Reporting all of them together rather than stopping at the first is deliberate: a
    graph defect usually shows up in more than one property, and seeing which ones fire
    is most of the diagnosis.

    ``notes`` carries what the run could not cover. It is separate from ``problems``
    because a coverage limit is not a violation, and it is printed rather than dropped
    because a guard that passes while quietly skipping part of its subject reads as
    stronger than it is.
    """
    baseline = baseline or latest_released_tag()
    problems: list[str] = []
    notes: list[str] = []

    disagreement = fresh_install_tables() ^ HEAD_TABLES
    if disagreement:
        problems.append(
            "storage.migrations.HEAD_TABLES disagrees with a fresh install on: "
            f"{', '.join(sorted(disagreement))}"
        )

    for slot, names in sorted(new_slot_collisions(baseline).items()):
        problems.append(f"slot {slot} did not collide in {baseline} and now does: {', '.join(sorted(names))}")

    problems.extend(rechained_revisions(baseline))
    problems.extend(edited_released_bodies(baseline))

    if include_upgrade:
        failures, unseeded = unrepairable_releases()
        for tag, reason in sorted(failures.items(), key=lambda item: version_key(item[0])):
            problems.append(f"{tag}: {reason}")
        if unseeded:
            notes.append(
                "no shipped schema accepted a generic row for these tables, so the upgrade ran "
                f"over them empty: {', '.join(sorted(unseeded))}"
            )

    return baseline, problems, notes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hold the migration graph to the shape its releases shipped.")
    parser.add_argument(
        "--baseline",
        help="release tag to compare metadata against (default: the newest release carrying migrations)",
    )
    parser.add_argument(
        "--skip-upgrade",
        action="store_true",
        help="check only the metadata properties, skipping the per-release upgrade",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        baseline, problems, notes = collect_problems(args.baseline, include_upgrade=not args.skip_upgrade)
    except MigrationGuardError as exc:
        print(f"migration release guard could not run: {exc}", file=sys.stderr)
        return 2

    for note in notes:
        print(f"note: {note}", file=sys.stderr)

    if problems:
        print(f"migration release guard failed (baseline {baseline}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"migration release guard passed (baseline {baseline})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
