"""add local resource access policies

Revision ID: 20260720_0030
Revises: 20260707_0029
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260720_0030"
down_revision = "20260707_0029"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return bind.exec_driver_sql(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (name,),
    ).first() is not None


def upgrade() -> None:
    if not _table_exists("resource_access_policies"):
        op.create_table(
            "resource_access_policies",
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=True),
            sa.Column("owner_user_id", sa.String(), nullable=True),
            sa.Column("owner_email", sa.String(), nullable=True),
            sa.Column("access_level", sa.String(), nullable=False, server_default="private"),
            sa.Column("created_by_user_id", sa.String(), nullable=True),
            sa.Column("updated_by_user_id", sa.String(), nullable=True),
            sa.Column("policy_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_applied_control_plane_revision", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "resource_kind in ('agent', 'vault_secret', 'skill', 'show_page')",
                name="ck_resource_access_policies_kind",
            ),
            sa.CheckConstraint(
                "access_level in ('public', 'scope', 'private')",
                name="ck_resource_access_policies_access_level",
            ),
            sa.PrimaryKeyConstraint("resource_kind", "resource_id"),
        )
    op.create_index(
        "ix_resource_access_policies_org_level",
        "resource_access_policies",
        ["organization_id", "access_level", "resource_kind"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_resource_access_policies_owner",
        "resource_access_policies",
        ["owner_user_id", "resource_kind"],
        if_not_exists=True,
    )

    if not _table_exists("resource_access_groups"):
        op.create_table(
            "resource_access_groups",
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=False),
            sa.Column("group_id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(
                ["resource_kind", "resource_id"],
                ["resource_access_policies.resource_kind", "resource_access_policies.resource_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("resource_kind", "resource_id", "group_id"),
        )
    op.create_index(
        "ix_resource_access_groups_group",
        "resource_access_groups",
        ["organization_id", "group_id", "resource_kind"],
        if_not_exists=True,
    )


def downgrade() -> None:
    if _table_exists("resource_access_groups"):
        op.drop_index("ix_resource_access_groups_group", table_name="resource_access_groups", if_exists=True)
        op.drop_table("resource_access_groups")
    op.drop_index("ix_resource_access_policies_owner", table_name="resource_access_policies", if_exists=True)
    op.drop_index("ix_resource_access_policies_org_level", table_name="resource_access_policies", if_exists=True)
    if _table_exists("resource_access_policies"):
        op.drop_table("resource_access_policies")
