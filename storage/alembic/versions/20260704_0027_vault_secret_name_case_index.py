"""enforce case-folded vault secret name uniqueness

Revision ID: 20260704_0027
Revises: 20260703_0026
Create Date: 2026-07-04
"""

from __future__ import annotations

from alembic import op

revision = "20260704_0027"
down_revision = "20260703_0026"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first() is not None


def _ensure_no_case_only_duplicates() -> None:
    bind = op.get_bind()
    duplicate = bind.exec_driver_sql(
        """
        select lower(name) as folded_name, group_concat(name, ', ') as names
        from vault_secrets
        group by lower(name)
        having count(*) > 1
        limit 1
        """
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot add vault secret folded-name uniqueness index; "
            f"case-only duplicates already exist for {duplicate[0]!r}: {duplicate[1]}"
        )


def upgrade() -> None:
    if not _table_exists("vault_secrets"):
        return
    _ensure_no_case_only_duplicates()
    op.get_bind().exec_driver_sql(
        "create unique index if not exists uq_vault_secrets_name_folded on vault_secrets (lower(name))"
    )


def downgrade() -> None:
    if _table_exists("vault_secrets"):
        op.drop_index("uq_vault_secrets_name_folded", table_name="vault_secrets", if_exists=True)
