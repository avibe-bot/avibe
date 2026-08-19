"""create the schema of a merged branch that was never applied

Revision ID: 20260819_0056
Revises: 20260817_0055
Create Date: 2026-08-19
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from alembic import op

revision = "20260819_0056"
down_revision = "20260817_0055"
branch_labels = None
depends_on = None

# 20260724_0034 forks. One side creates the local access-control and remote
# authorization tables below; the other ends at 20260804_0046. 20260804_0047 merges
# them. A database that reached the merge along the other side alone crossed it with
# this side's revisions recorded as applied and its tables never created, and Alembic
# will not walk back below a merge it has passed. The gap stayed dormant until
# 20260815_0054 tried to alter one of the missing tables, which aborts the whole
# upgrade: those installs are pinned below head and every caller of
# ``ensure_sqlite_state`` fails, including the login that writes an authorization.
_UNAPPLIED_BRANCH = (
    "20260725_0035_resource_access_policies",
    "20260725_0036_project_access_policies",
    "20260725_0037_media_object_references",
    "20260725_0038_remote_access_authorizations",
    # Replayed last so a table restored above reaches the shape head expects
    # rather than the shape it had in July.
    "20260815_0054_remote_authorization_context",
)

_BRANCH_TABLES = (
    "resource_access_policies",
    "resource_access_groups",
    "project_access_policies",
    "project_access_bindings",
    "media_object_references",
    "remote_access_authorizations",
)


def _revision_module(stem: str) -> ModuleType:
    path = Path(__file__).with_name(f"{stem}.py")
    spec = importlib.util.spec_from_file_location(f"_replay_{stem}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"cannot load migration {stem} for replay")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _missing_tables() -> set[str]:
    present = {
        str(row[0])
        for row in op.get_bind().exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }
    return {table for table in _BRANCH_TABLES if table not in present}


def upgrade() -> None:
    if not _missing_tables():
        return

    # Replay rather than copy their DDL: each of these revisions already skips every
    # object it finds in place, so the repaired schema equals a fresh install's by
    # construction instead of by a duplicate definition that can drift away from it.
    for stem in _UNAPPLIED_BRANCH:
        _revision_module(stem).upgrade()

    # Replaying is not the same as repairing. 20260725_0037 returns silently when
    # media_objects or agent_sessions is absent, and a revision added to the branch
    # later can grow its own precondition, so the replay can leave a table behind
    # while reporting nothing. Failing here keeps the database below head, where the
    # next upgrade retries the repair; stamping this revision instead would record a
    # half-repaired schema as complete and surface it later as an unattributable
    # error at whichever call site touches the table that was never created.
    unrepaired = _missing_tables()
    if unrepaired:
        raise RuntimeError(
            "replaying the unapplied branch left these tables missing: "
            f"{', '.join(sorted(unrepaired))}; a replayed revision skipped itself "
            "because one of its preconditions is absent from this database"
        )


def downgrade() -> None:
    # Deliberately empty. This revision creates only what is already missing, and
    # everything it creates is owned by revisions below it. Dropping those tables
    # here would delete the authorization and access-policy rows that a correctly
    # migrated database has held since July.
    return
