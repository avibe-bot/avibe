"""vault tables (secret management)

Revision ID: 20260621_0023
Revises: 20260622_0023
Create Date: 2026-06-21 (re-parented onto 20260622_0023 to linearize after master)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260621_0023"
down_revision = "20260622_0023"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    bind = op.get_bind()
    return {row[0] for row in bind.exec_driver_sql("select name from sqlite_master where type = 'table'")}


def upgrade() -> None:
    existing = _tables()

    if "vault_secrets" not in existing:
        op.create_table(
            "vault_secrets",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("tags", sa.Text(), nullable=True),
            sa.Column("kind", sa.String(), nullable=False, server_default="static"),
            sa.Column("protection", sa.String(), nullable=False, server_default="standard"),
            sa.Column("signer_kind", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=False, server_default="manual"),
            sa.Column("ciphertext", sa.Text(), nullable=True),
            sa.Column("nonce", sa.Text(), nullable=True),
            sa.Column("wrap_meta", sa.Text(), nullable=True),
            sa.Column("public_meta", sa.Text(), nullable=True),
            sa.Column("policy", sa.Text(), nullable=True),
            sa.Column("last_used_at", sa.String(), nullable=True),
            sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.UniqueConstraint("name", name="uq_vault_secrets_name"),
        )
    op.create_index("ix_vault_secrets_name_kind", "vault_secrets", ["name", "kind"], if_not_exists=True)

    if "vault_requests" not in existing:
        op.create_table(
            "vault_requests",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("request_type", sa.String(), nullable=False),
            sa.Column("secret_name", sa.String(), nullable=True),
            sa.Column("requester", sa.Text(), nullable=True),
            sa.Column("delivery", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("message_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("decided_at", sa.String(), nullable=True),
            sa.Column("expires_at", sa.String(), nullable=True),
        )
    op.create_index("ix_vault_requests_status_created", "vault_requests", ["status", "created_at"], if_not_exists=True)

    if "vault_grants" not in existing:
        op.create_table(
            "vault_grants",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("member_snapshot", sa.Text(), nullable=False),
            sa.Column("source_selector", sa.Text(), nullable=True),
            sa.Column("request_id", sa.String(), nullable=True),
            sa.Column("session_id", sa.String(), nullable=True),
            sa.Column("purpose", sa.String(), nullable=False, server_default="run"),
            sa.Column("status", sa.String(), nullable=False, server_default="active"),
            sa.Column("one_shot", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("revoked_at", sa.String(), nullable=True),
            sa.Column("agent_ready", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("agent_ready_at", sa.String(), nullable=True),
        )
    op.create_index("ix_vault_grants_status_expires", "vault_grants", ["status", "expires_at"], if_not_exists=True)
    op.create_index("ix_vault_grants_request", "vault_grants", ["request_id"], if_not_exists=True)
    op.create_index("ix_vault_grants_session_purpose", "vault_grants", ["session_id", "purpose"], if_not_exists=True)

    if "vault_audit" not in existing:
        op.create_table(
            "vault_audit",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("ts", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("secret_name", sa.String(), nullable=True),
            sa.Column("requester", sa.Text(), nullable=True),
            sa.Column("delivery", sa.Text(), nullable=True),
            sa.Column("request_id", sa.String(), nullable=True),
            sa.Column("grant_id", sa.String(), nullable=True),
        )
    op.create_index("ix_vault_audit_ts", "vault_audit", ["ts"], if_not_exists=True)
    op.create_index("ix_vault_audit_secret_ts", "vault_audit", ["secret_name", "ts"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_vault_audit_secret_ts", table_name="vault_audit", if_exists=True)
    op.drop_index("ix_vault_audit_ts", table_name="vault_audit", if_exists=True)
    op.drop_table("vault_audit")
    op.drop_index("ix_vault_grants_session_purpose", table_name="vault_grants", if_exists=True)
    op.drop_index("ix_vault_grants_request", table_name="vault_grants", if_exists=True)
    op.drop_index("ix_vault_grants_status_expires", table_name="vault_grants", if_exists=True)
    op.drop_table("vault_grants")
    op.drop_index("ix_vault_requests_status_created", table_name="vault_requests", if_exists=True)
    op.drop_table("vault_requests")
    op.drop_index("ix_vault_secrets_name_kind", table_name="vault_secrets", if_exists=True)
    op.drop_table("vault_secrets")
