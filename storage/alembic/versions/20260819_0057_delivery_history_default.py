"""restore a usable default for message_deliveries.delivery_history_json

Revision ID: 20260819_0057
Revises: 20260819_0056
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260819_0057"
down_revision = "20260819_0056"
branch_labels = None
depends_on = None

_TABLE = "message_deliveries"
_COLUMN = "delivery_history_json"

# 20260801_0044 declared this default as the Python string '{"version":1,"events":[]}'.
# That string compiles correctly on its own -- the column shipped usable. 20260811_0050
# then rebuilt the table through ``op.batch_alter_table`` to replace a check
# constraint. Batch mode reflects the existing table, reflection hands a server default
# back as a ``TextClause``, and compiling a ``TextClause`` treats ``:1`` as a bind
# parameter: the emitted DDL became DEFAULT '{"version"NULL,"events":[]}'. Every
# database that crossed 0050 stores that, and it is not valid JSON, so
# ck_message_deliveries_history_json rejects any insert that omits the column -- the
# one thing a defaulted column exists to allow.
#
# The repair is not merely the correct literal: re-emitting a literal keeps the column
# one rebuild away from the same corruption. ``json_object()`` carries no colon at all,
# so it survives any number of reflect-and-recompile cycles while evaluating to the
# same bytes the application writes.
_FIXED_DEFAULT = "(json_object('version', 1, 'events', json_array()))"

# What 0050 left behind, restored verbatim on downgrade. Undoing this migration means
# putting back the shape the previous revision actually shipped; quietly leaving the
# corrected default would make 0057 irreversible and hide where the fix came from.
# Reintroducing it is inert -- every write path supplies the column explicitly, which
# is why the defect stayed latent -- and no released code reads the default at all.
_CORRUPTED_DEFAULT = "'{\"version\"NULL,\"events\":[]}'"

# 0050 rebuilt this table the same way and is released, so its helper cannot be shared
# from here without editing a shipped body. session_turns references
# message_deliveries, and batch mode rebuilds by copy-drop-rename, so the rebuild runs
# with foreign keys off and is verified before the connection re-enables them.
_FK_NAMING_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _set_default(expression: str) -> None:
    with op.batch_alter_table(_TABLE, naming_convention=_FK_NAMING_CONVENTION) as batch:
        batch.alter_column(
            _COLUMN,
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text(expression),
        )


def _survey(bind) -> tuple[int, int]:
    """Row count and dangling-reference count this migration must not change."""
    rows = bind.exec_driver_sql(f"select count(*) from {_TABLE}").scalar_one()
    dangling = len(bind.exec_driver_sql(f"PRAGMA foreign_key_check({_TABLE})").fetchall())
    return rows, dangling


def _rebuild(expression: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        _set_default(expression)
        return
    with op.get_context().autocommit_block():
        # Batch mode rebuilds by copy-drop-rename, and four tables reference this one,
        # so the drop needs foreign keys off. What that suspends has to be re-checked
        # afterwards -- but against what this migration is answerable for, not against
        # an absolute. A database may already hold a dangling reference (SQLite
        # enforces none unless the connection asks), and refusing to upgrade over
        # damage predating this revision would pin the install below head for a reason
        # the migration did not cause. So both bounds come from measuring the same
        # table before and after: rows cannot be lost, and no reference this rebuild
        # touched may come loose. Row preservation is what covers the four referring
        # tables -- their references can only break if a row stops existing.
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            before = _survey(bind)
            _set_default(expression)
            after = _survey(bind)
        finally:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        if after != before:
            raise RuntimeError(
                f"delivery history default rebuild altered {_TABLE}: "
                f"(rows, dangling refs) {before} -> {after}"
            )


def upgrade() -> None:
    _rebuild(_FIXED_DEFAULT)


def downgrade() -> None:
    _rebuild(_CORRUPTED_DEFAULT)
