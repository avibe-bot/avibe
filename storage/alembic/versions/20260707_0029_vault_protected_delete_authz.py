"""vault protected delete WebAuthn authz

Revision ID: 20260707_0029
Revises: 20260705_0028
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260707_0029"
down_revision = "20260705_0028"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first() is not None


def upgrade() -> None:
    if not _table_exists("vault_auth_factors"):
        op.create_table(
            "vault_auth_factors",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("label", sa.Text(), nullable=True),
            sa.Column("rp_id", sa.String(), nullable=False),
            sa.Column("credential_id", sa.Text(), nullable=False),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("alg", sa.Integer(), nullable=False),
            sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transports", sa.Text(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("last_used_at", sa.String(), nullable=True),
            sa.Column("disabled_at", sa.String(), nullable=True),
            sa.UniqueConstraint("credential_id", name="uq_vault_auth_factors_credential_id"),
        )
    op.create_index(
        "ix_vault_auth_factors_kind_rp",
        "vault_auth_factors",
        ["kind", "rp_id", "disabled_at"],
        if_not_exists=True,
    )

    if not _table_exists("vault_operation_challenges"):
        op.create_table(
            "vault_operation_challenges",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("operation", sa.String(), nullable=False),
            sa.Column("secret_name", sa.String(), nullable=True),
            sa.Column("secret_id", sa.String(), nullable=True),
            sa.Column("secret_updated_at", sa.String(), nullable=True),
            sa.Column("challenge_hash", sa.String(), nullable=False),
            sa.Column("rp_id", sa.String(), nullable=False),
            sa.Column("origin", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("consumed_at", sa.String(), nullable=True),
            sa.Column("factor_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
        )
    op.create_index(
        "ix_vault_operation_challenges_lookup",
        "vault_operation_challenges",
        ["operation", "secret_name", "expires_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_vault_operation_challenges_consumed",
        "vault_operation_challenges",
        ["consumed_at", "expires_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_vault_operation_challenges_consumed", table_name="vault_operation_challenges", if_exists=True)
    op.drop_index("ix_vault_operation_challenges_lookup", table_name="vault_operation_challenges", if_exists=True)
    if _table_exists("vault_operation_challenges"):
        op.drop_table("vault_operation_challenges")
    op.drop_index("ix_vault_auth_factors_kind_rp", table_name="vault_auth_factors", if_exists=True)
    if _table_exists("vault_auth_factors"):
        op.drop_table("vault_auth_factors")
