"""retain positive native steer receipts across materialization failures

Revision ID: 20260811_0050
Revises: 20260809_0049
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260811_0050"
down_revision = "20260809_0049"
branch_labels = None
depends_on = None

_CHECK = "ck_message_deliveries_current_receipt"
_UPGRADE_CHECK = (
    "(state = 'reconciling_steer' "
    "and current_receipt_outcome in ('accepted', 'unknown')) "
    "or (state <> 'reconciling_steer' "
    "and current_receipt_outcome is null)"
)
_DOWNGRADE_CHECK = (
    "(state = 'reconciling_steer' "
    "and current_receipt_outcome = 'unknown') "
    "or (state <> 'reconciling_steer' "
    "and current_receipt_outcome is null)"
)


def _check_names(bind) -> set[str]:
    return {
        str(check["name"])
        for check in sa.inspect(bind).get_check_constraints("message_deliveries")
        if check.get("name")
    }


def _replace_check(bind, expression: str) -> None:
    with op.batch_alter_table(
        "message_deliveries",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
        },
    ) as batch:
        if _CHECK in _check_names(bind):
            batch.drop_constraint(_CHECK, type_="check")
        batch.create_check_constraint(_CHECK, expression)


def _sqlite_rebuild(operation) -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            operation(bind)
        finally:
            bind.exec_driver_sql("PRAGMA foreign_keys=ON")
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "accepted steer receipt migration left invalid SQLite foreign keys"
            )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        _sqlite_rebuild(lambda current_bind: _replace_check(current_bind, _UPGRADE_CHECK))
    else:
        _replace_check(bind, _UPGRADE_CHECK)


def downgrade() -> None:
    bind = op.get_bind()
    accepted_rows = bind.execute(
        sa.text(
            "select count(*) from message_deliveries "
            "where state = 'reconciling_steer' "
            "and current_receipt_outcome = 'accepted'"
        )
    ).scalar_one()
    if accepted_rows:
        raise RuntimeError(
            "0050 downgrade refused: accepted steer receipt evidence cannot be represented"
        )
    if bind.dialect.name == "sqlite":
        _sqlite_rebuild(
            lambda current_bind: _replace_check(current_bind, _DOWNGRADE_CHECK)
        )
    else:
        _replace_check(bind, _DOWNGRADE_CHECK)
