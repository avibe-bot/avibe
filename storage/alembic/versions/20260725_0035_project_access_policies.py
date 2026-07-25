"""add local Project access policies and bindings

Revision ID: 20260725_0035
Revises: 20260724_0034
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260725_0035"
down_revision = "20260724_0034"
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
    bind = op.get_bind()
    tables = _tables(bind)
    if "project_access_policies" not in tables:
        op.create_table(
            "project_access_policies",
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("scope_id", sa.String(), nullable=False),
            sa.Column("organization_id", sa.String(), nullable=True),
            sa.Column("mode", sa.String(), server_default="inherit", nullable=False),
            sa.Column("policy_revision", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "last_applied_control_plane_revision",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "last_applied_control_plane_revision >= 0",
                name="ck_project_access_policies_control_revision",
            ),
            sa.CheckConstraint(
                "mode in ('inherit', 'restricted')",
                name="ck_project_access_policies_mode",
            ),
            sa.CheckConstraint(
                "policy_revision >= 0",
                name="ck_project_access_policies_revision",
            ),
            sa.ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("project_id"),
            sa.UniqueConstraint("scope_id", name="uq_project_access_policies_scope"),
        )
        op.create_index(
            "ix_project_access_policies_organization",
            "project_access_policies",
            ["organization_id"],
        )

    tables = _tables(bind)
    if "project_access_bindings" not in tables:
        op.create_table(
            "project_access_bindings",
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("principal_kind", sa.String(), nullable=False),
            sa.Column("principal_value", sa.String(), nullable=False),
            sa.Column("access_role", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.CheckConstraint(
                "access_role in ('editor', 'viewer')",
                name="ck_project_access_bindings_role",
            ),
            sa.CheckConstraint(
                "principal_kind in ('email', 'email_domain', 'organization_group')",
                name="ck_project_access_bindings_kind",
            ),
            sa.ForeignKeyConstraint(
                ["project_id"],
                ["project_access_policies.project_id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("project_id", "principal_kind", "principal_value"),
        )
        op.create_index(
            "ix_project_access_bindings_principal",
            "project_access_bindings",
            ["principal_kind", "principal_value"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "project_access_bindings" in tables:
        op.drop_index(
            "ix_project_access_bindings_principal",
            table_name="project_access_bindings",
        )
        op.drop_table("project_access_bindings")
    if "project_access_policies" in tables:
        op.drop_index(
            "ix_project_access_policies_organization",
            table_name="project_access_policies",
        )
        op.drop_table("project_access_policies")
