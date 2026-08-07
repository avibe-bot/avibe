"""store large remote authorization claims outside browser cookies

Revision ID: 20260725_0038
Revises: 20260725_0037
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0038"
down_revision = "20260725_0037"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def upgrade() -> None:
    if "remote_access_authorizations" in _tables(op.get_bind()):
        return
    op.create_table(
        "remote_access_authorizations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("claims_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_remote_access_authorizations_expires",
        "remote_access_authorizations",
        ["expires_at"],
    )


def downgrade() -> None:
    if "remote_access_authorizations" not in _tables(op.get_bind()):
        return
    op.drop_index(
        "ix_remote_access_authorizations_expires",
        table_name="remote_access_authorizations",
    )
    op.drop_table("remote_access_authorizations")
