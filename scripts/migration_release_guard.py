#!/usr/bin/env python3
"""Hold the Alembic migration graph to the shape its releases already shipped.

A released migration graph is a shipped surface. Databases in the field were built by it,
they record only the revision they reached, and Alembic walks them forward from there and
never back. So editing a released revision's parentage does not change what those
databases have already done -- it changes what they will do next, silently, with no error
at the moment of the edit and no error at the moment of the upgrade.

Four properties, one primitive: the graph as a released tag actually shipped it, read
straight out of git. Nothing here is a hand-maintained list of known-bad cases, and
nothing needs an old Python environment -- ``env.py`` reads ``target_metadata`` only
during autogenerate, so today's runtime can drive an older ``version_locations``.

    fresh_install_tables()        HEAD_TABLES is what a fresh install actually has, so
                                  the property below derives from something measured
    new_slot_collisions()         a change introduces no new duplicated slot number
    rechained_revisions()         a released revision keeps its identity and its parent
    unrepairable_releases()       a database built by a released graph still reaches the
                                  full schema when today's graph upgrades it

The first three are metadata only. The fourth builds a database per release and costs
roughly a fifth of a second each.

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

from storage.migrations import HEAD_TABLES, alembic_dir

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


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise MigrationGuardError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def version_key(tag: str) -> tuple[int, ...]:
    """Sort key for a ``vX.Y.Z`` tag; empty for anything that is not one."""
    try:
        return tuple(int(part) for part in tag.lstrip("v").split("."))
    except ValueError:
        return ()


def _carries_migrations(tag: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{tag}:{VERSIONS_PATH}"],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
    )
    return result.returncode == 0


def released_tags() -> list[str]:
    """Every release tag that shipped a migrations directory, oldest first.

    The window is derived, not chosen. A tag without ``storage/alembic/versions`` never
    put a migrated database in the field; every tag with one did. A "last N releases"
    cutoff would instead stop covering a release on a schedule unrelated to whether
    anyone is still running it.
    """
    candidates = sorted((tag for tag in _git("tag", "-l", "v*").split() if version_key(tag)), key=version_key)
    tags = [tag for tag in candidates if _carries_migrations(tag)]
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
    """Slots that collide now and did not collide in the baseline release.

    The collisions history already contains are in the baseline itself, so they need no
    allowlist -- and cannot decay into one that outlives the reason it was written.
    """
    baseline = baseline or latest_released_tag()
    shipped = slot_collisions(released_sources(baseline))
    return {slot: names for slot, names in slot_collisions(working_tree_sources()).items() if slot not in shipped}


def revision_graph(sources: dict[str, str]) -> dict[str, tuple[str, object]]:
    """``{revision: (filename, down_revision)}``, parsed rather than imported.

    Parsing keeps this free of import side effects and lets it read a revision from a
    release whose modules would no longer import against today's code. A revision that
    computes either value instead of assigning a literal reads as ``"<computed>"``, which
    compares unequal to itself and so is reported rather than skipped.
    """
    graph: dict[str, tuple[str, object]] = {}
    for name, source in sources.items():
        assigned: dict[str, object] = {}
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                try:
                    assigned[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    assigned[target.id] = "<computed>"
        if "revision" in assigned:
            graph[str(assigned["revision"])] = (name, assigned.get("down_revision"))
    return graph


def rechained_revisions(baseline: str | None = None) -> list[str]:
    """Released revisions whose identity or parent has changed since the baseline release.

    This is the property the outage broke. Inserting an ancestor behind a revision the
    field has already passed is invisible to Alembic, which only walks forward, and
    invisible to a single-head assertion, which a merge revision satisfies. It is visible
    here because the shipped parent is read from the release rather than inferred from
    the graph that replaced it.
    """
    baseline = baseline or latest_released_tag()
    current = revision_graph(working_tree_sources())
    problems = []
    for revision, (name, down_revision) in sorted(revision_graph(released_sources(baseline)).items()):
        if revision not in current:
            problems.append(f"{revision} ({name}) shipped in {baseline} and is no longer in the graph")
        elif current[revision][1] != down_revision:
            problems.append(
                f"{revision} ({name}) shipped in {baseline} with down_revision={down_revision!r} "
                f"and now has {current[revision][1]!r}"
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
    """The tables a database has after today's graph builds it from empty."""
    with tempfile.TemporaryDirectory() as workspace:
        db_path = Path(workspace) / "vibe.sqlite"
        command.upgrade(_alembic_config(db_path), "head")
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


def missing_tables_after_upgrade(tag: str) -> set[str]:
    """Tables still absent after a database built by ``tag``'s graph is upgraded by today's.

    Whatever Alembic raises when the upgrade itself aborts propagates: a crash and a
    silently incomplete schema are the same failure of the same property, and the caller
    records them the same way.
    """
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        db_path = root / "vibe.sqlite"
        released = extract_released_versions(tag, root / "released")
        command.upgrade(_alembic_config(db_path, released), "head")

        # An Alembic run that resolves ``version_locations`` to nothing reports success
        # and applies nothing, and an empty database then upgrades cleanly to today's head
        # -- the guard's own false-negative. Confirm the first upgrade really walked the
        # released graph before believing anything the second one produces.
        stamp = stamped_revision(db_path)
        if stamp is None or stamp not in revision_graph(released_sources(tag)):
            raise MigrationGuardError(
                f"upgrading with {tag}'s graph left the database stamped {stamp!r}, which is not "
                f"one of its revisions; the released graph was not applied"
            )

        command.upgrade(_alembic_config(db_path), "head")
        return HEAD_TABLES - table_names(db_path)


def unrepairable_releases() -> dict[str, str]:
    """``{tag: reason}`` for every release whose databases today's graph cannot bring to head."""
    failures = {}
    for tag in released_tags():
        try:
            missing = missing_tables_after_upgrade(tag)
        except Exception as exc:  # noqa: BLE001 - however the upgrade fails, the verdict is the same
            failures[tag] = f"upgrade aborted with {type(exc).__name__}: {str(exc).splitlines()[0]}"
        else:
            if missing:
                failures[tag] = f"upgrade left {len(missing)} table(s) missing: {', '.join(sorted(missing))}"
    return failures


def collect_problems(baseline: str | None = None, *, include_upgrade: bool = True) -> tuple[str, list[str]]:
    """``(baseline, problems)`` -- every property checked, every violation reported at once.

    Reporting all of them together rather than stopping at the first is deliberate: a
    graph defect usually shows up in more than one property, and seeing which ones fire
    is most of the diagnosis.
    """
    baseline = baseline or latest_released_tag()
    problems: list[str] = []

    disagreement = fresh_install_tables() ^ HEAD_TABLES
    if disagreement:
        problems.append(
            "storage.migrations.HEAD_TABLES disagrees with a fresh install on: "
            f"{', '.join(sorted(disagreement))}"
        )

    for slot, names in sorted(new_slot_collisions(baseline).items()):
        problems.append(f"slot {slot} did not collide in {baseline} and now does: {', '.join(sorted(names))}")

    problems.extend(rechained_revisions(baseline))

    if include_upgrade:
        for tag, reason in sorted(unrepairable_releases().items(), key=lambda item: version_key(item[0])):
            problems.append(f"{tag}: {reason}")

    return baseline, problems


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
        baseline, problems = collect_problems(args.baseline, include_upgrade=not args.skip_upgrade)
    except MigrationGuardError as exc:
        print(f"migration release guard could not run: {exc}", file=sys.stderr)
        return 2

    if problems:
        print(f"migration release guard failed (baseline {baseline}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"migration release guard passed (baseline {baseline})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
