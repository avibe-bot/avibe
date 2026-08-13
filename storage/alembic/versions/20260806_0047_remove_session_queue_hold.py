"""remove the Session queue hold policy

Revision ID: 20260806_0047
Revises: 20260804_0046
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260806_0047"
down_revision = "20260804_0046"
branch_labels = None
depends_on = None

_COLUMNS = ("queue_hold_state", "queue_hold_version", "queue_held_at")
_CHECK = "ck_agent_sessions_queue_hold_state"


def _column_names(bind) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(bind).get_columns("agent_sessions")
    }


def _check_names(bind) -> set[str]:
    return {
        str(check["name"])
        for check in sa.inspect(bind).get_check_constraints("agent_sessions")
        if check.get("name")
    }


def _drop_hold_columns(bind) -> None:
    columns = _column_names(bind)
    checks = _check_names(bind)
    with op.batch_alter_table("agent_sessions") as batch:
        if _CHECK in checks:
            batch.drop_constraint(_CHECK, type_="check")
        for column in reversed(_COLUMNS):
            if column in columns:
                batch.drop_column(column)


def _restore_hold_columns(bind) -> None:
    columns = _column_names(bind)
    with op.batch_alter_table("agent_sessions") as batch:
        if "queue_hold_state" not in columns:
            batch.add_column(
                sa.Column(
                    "queue_hold_state",
                    sa.String(),
                    server_default="open",
                    nullable=False,
                )
            )
        if "queue_hold_version" not in columns:
            batch.add_column(
                sa.Column(
                    "queue_hold_version",
                    sa.Integer(),
                    server_default="1",
                    nullable=False,
                )
            )
        if "queue_held_at" not in columns:
            batch.add_column(sa.Column("queue_held_at", sa.String(), nullable=True))
        if _CHECK not in _check_names(bind):
            batch.create_check_constraint(
                _CHECK,
                "queue_hold_state in ('open', 'held')",
            )


def _sqlite_rebuild(operation) -> None:
    bind = op.get_bind()
    # SQLite cannot rebuild a referenced parent table while FK enforcement is
    # enabled. Keep the exception local to this autocommit block, restore it
    # immediately, and fail if the rebuilt schema has any dangling references.
    with op.get_context().autocommit_block():
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            operation(bind)
        finally:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("queue hold removal left invalid SQLite foreign keys")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_rebuild(_drop_hold_columns)
    else:
        _drop_hold_columns(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_rebuild(_restore_hold_columns)
    else:
        _restore_hold_columns(bind)
