"""add organization-aware Harness authorization state

Revision ID: 20260728_0039
Revises: 20260725_0038
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260728_0039"
down_revision = "20260725_0038"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    bind = op.get_bind()
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _columns(table: str) -> set[str]:
    return {
        str(row[1])
        for row in op.get_bind().exec_driver_sql(f'pragma table_info("{table}")')
    }


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = _tables()
    if "run_definitions" in tables:
        _add_column("run_definitions", sa.Column("project_id", sa.String(), nullable=True))
        _add_column(
            "run_definitions",
            sa.Column(
                "authorization_state",
                sa.String(),
                nullable=False,
                server_default="active",
            ),
        )
        op.create_index(
            "ix_run_definitions_project",
            "run_definitions",
            ["project_id"],
            if_not_exists=True,
        )
        # Definitions created before Harness authorization have no reliable
        # execution principal or dependency attribution. Require an authorized
        # owner to explicitly resume them, which refreshes both before use.
        op.execute(
            """
            UPDATE run_definitions
               SET enabled = 0,
                   authorization_state = 'suspended_authorization'
             WHERE deleted_at IS NULL
            """
        )

    if "agent_runs" in tables:
        _add_column("agent_runs", sa.Column("project_id", sa.String(), nullable=True))
        _add_column(
            "agent_runs",
            sa.Column(
                "authorization_provenance_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
        )
        _add_column("agent_runs", sa.Column("member_safe_json", sa.Text(), nullable=True))
        _add_column(
            "agent_runs",
            sa.Column(
                "output_classification",
                sa.String(),
                nullable=False,
                server_default="unclassified",
            ),
        )
        _add_column(
            "agent_runs",
            sa.Column(
                "output_quarantined",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        _add_column("agent_runs", sa.Column("safe_error_code", sa.String(), nullable=True))
        op.create_index(
            "ix_agent_runs_project_created",
            "agent_runs",
            ["project_id", "created_at"],
            if_not_exists=True,
        )

    tables = _tables()
    if "harness_principal_entitlements" not in tables:
        op.create_table(
            "harness_principal_entitlements",
            sa.Column("instance_id", sa.String(), nullable=False),
            sa.Column("subject", sa.String(), nullable=False),
            sa.Column("organization_member_id", sa.String(), nullable=True),
            sa.Column("email", sa.String(), nullable=True),
            sa.Column("organization_id", sa.String(), nullable=True),
            sa.Column("instance_role", sa.String(), nullable=False),
            sa.Column("instance_access_source", sa.String(), nullable=True),
            sa.Column("organization_role", sa.String(), nullable=True),
            sa.Column("group_ids_json", sa.Text(), nullable=False),
            sa.Column("membership_version", sa.String(), nullable=True),
            sa.Column("authorization_revision", sa.Integer(), nullable=False),
            sa.Column("claims_issued_at", sa.Integer(), nullable=False),
            sa.Column("fresh_until", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.PrimaryKeyConstraint("instance_id", "subject"),
        )
        op.create_index(
            "ix_harness_principal_entitlements_fresh",
            "harness_principal_entitlements",
            ["fresh_until"],
        )
        op.create_index(
            "ix_harness_principal_entitlements_member",
            "harness_principal_entitlements",
            ["organization_id", "organization_member_id"],
        )

    if "harness_definition_dependencies" not in tables:
        op.create_table(
            "harness_definition_dependencies",
            sa.Column("definition_id", sa.String(), nullable=False),
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=False),
            sa.Column("access_mode", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(
                ["definition_id"],
                ["run_definitions.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("definition_id", "resource_kind", "resource_id"),
        )
        op.create_index(
            "ix_harness_definition_dependencies_resource",
            "harness_definition_dependencies",
            ["resource_kind", "resource_id"],
        )

    if "harness_run_dependencies" not in tables:
        op.create_table(
            "harness_run_dependencies",
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=False),
            sa.Column("access_mode", sa.String(), nullable=False),
            sa.Column("used_at", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("run_id", "resource_kind", "resource_id"),
        )
        op.create_index(
            "ix_harness_run_dependencies_resource",
            "harness_run_dependencies",
            ["resource_kind", "resource_id", "run_id"],
        )

    # SQLite cannot alter a CHECK constraint in place and released databases
    # contain both named and unnamed versions of this constraint. Recreate the
    # table explicitly so upgrades do not depend on reflected constraint names.
    if "resource_access_policies" in tables:
        bind = op.get_bind()
        bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
        try:
            if "resource_access_groups" in tables:
                bind.exec_driver_sql(
                    """
                    CREATE TEMP TABLE resource_access_groups_harness_backup AS
                    SELECT resource_kind, resource_id, group_id,
                           organization_id, created_at
                      FROM resource_access_groups
                    """
                )
            bind.exec_driver_sql(
                """
                CREATE TABLE resource_access_policies_harness_tmp (
                    resource_kind VARCHAR NOT NULL,
                    resource_id VARCHAR NOT NULL,
                    organization_id VARCHAR,
                    owner_user_id VARCHAR,
                    owner_email VARCHAR,
                    access_level VARCHAR NOT NULL,
                    created_by_user_id VARCHAR,
                    updated_by_user_id VARCHAR,
                    policy_revision INTEGER NOT NULL DEFAULT 0,
                    last_applied_control_plane_revision INTEGER,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL,
                    PRIMARY KEY (resource_kind, resource_id),
                    CONSTRAINT ck_resource_access_policies_kind CHECK (
                        resource_kind IN (
                            'agent', 'vault_secret', 'skill', 'show_page',
                            'harness_task', 'harness_watch'
                        )
                    ),
                    CONSTRAINT ck_resource_access_policies_access_level CHECK (
                        access_level IN ('public', 'scope', 'private')
                    )
                )
                """
            )
            bind.exec_driver_sql(
                """
                INSERT INTO resource_access_policies_harness_tmp
                SELECT resource_kind, resource_id, organization_id,
                       owner_user_id, owner_email, access_level,
                       created_by_user_id, updated_by_user_id,
                       policy_revision, last_applied_control_plane_revision,
                       created_at, updated_at
                  FROM resource_access_policies
                """
            )
            bind.exec_driver_sql("DROP TABLE resource_access_policies")
            bind.exec_driver_sql(
                "ALTER TABLE resource_access_policies_harness_tmp "
                "RENAME TO resource_access_policies"
            )
            bind.exec_driver_sql(
                "CREATE INDEX ix_resource_access_policies_org_level "
                "ON resource_access_policies "
                "(organization_id, access_level, resource_kind)"
            )
            bind.exec_driver_sql(
                "CREATE INDEX ix_resource_access_policies_owner "
                "ON resource_access_policies (owner_user_id, resource_kind)"
            )
            if "resource_access_groups" in tables:
                bind.exec_driver_sql(
                    """
                    INSERT INTO resource_access_groups (
                        resource_kind, resource_id, group_id,
                        organization_id, created_at
                    )
                    SELECT resource_kind, resource_id, group_id,
                           organization_id, created_at
                      FROM resource_access_groups_harness_backup
                    """
                )
                bind.exec_driver_sql(
                    "DROP TABLE resource_access_groups_harness_backup"
                )
        finally:
            bind.exec_driver_sql("PRAGMA defer_foreign_keys = OFF")


def _restore_pre_harness_resource_policies(tables: set[str]) -> None:
    if "resource_access_policies" not in tables:
        return
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    try:
        if "resource_access_groups" in tables:
            bind.exec_driver_sql(
                """
                CREATE TEMP TABLE resource_access_groups_pre_harness_backup AS
                SELECT resource_kind, resource_id, group_id,
                       organization_id, created_at
                  FROM resource_access_groups
                 WHERE resource_kind IN (
                    'agent', 'vault_secret', 'skill', 'show_page'
                 )
                """
            )
        bind.exec_driver_sql(
            """
            CREATE TABLE resource_access_policies_pre_harness_tmp (
                resource_kind VARCHAR NOT NULL,
                resource_id VARCHAR NOT NULL,
                organization_id VARCHAR,
                owner_user_id VARCHAR,
                owner_email VARCHAR,
                access_level VARCHAR NOT NULL,
                created_by_user_id VARCHAR,
                updated_by_user_id VARCHAR,
                policy_revision INTEGER NOT NULL DEFAULT 0,
                last_applied_control_plane_revision INTEGER,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                PRIMARY KEY (resource_kind, resource_id),
                CONSTRAINT ck_resource_access_policies_kind CHECK (
                    resource_kind IN (
                        'agent', 'vault_secret', 'skill', 'show_page'
                    )
                ),
                CONSTRAINT ck_resource_access_policies_access_level CHECK (
                    access_level IN ('public', 'scope', 'private')
                )
            )
            """
        )
        bind.exec_driver_sql(
            """
            INSERT INTO resource_access_policies_pre_harness_tmp
            SELECT resource_kind, resource_id, organization_id,
                   owner_user_id, owner_email, access_level,
                   created_by_user_id, updated_by_user_id,
                   policy_revision, last_applied_control_plane_revision,
                   created_at, updated_at
              FROM resource_access_policies
             WHERE resource_kind IN (
                'agent', 'vault_secret', 'skill', 'show_page'
             )
            """
        )
        bind.exec_driver_sql("DROP TABLE resource_access_policies")
        bind.exec_driver_sql(
            "ALTER TABLE resource_access_policies_pre_harness_tmp "
            "RENAME TO resource_access_policies"
        )
        bind.exec_driver_sql(
            "CREATE INDEX ix_resource_access_policies_org_level "
            "ON resource_access_policies "
            "(organization_id, access_level, resource_kind)"
        )
        bind.exec_driver_sql(
            "CREATE INDEX ix_resource_access_policies_owner "
            "ON resource_access_policies (owner_user_id, resource_kind)"
        )
        if "resource_access_groups" in tables:
            bind.exec_driver_sql(
                """
                INSERT INTO resource_access_groups (
                    resource_kind, resource_id, group_id,
                    organization_id, created_at
                )
                SELECT resource_kind, resource_id, group_id,
                       organization_id, created_at
                  FROM resource_access_groups_pre_harness_backup
                """
            )
            bind.exec_driver_sql(
                "DROP TABLE resource_access_groups_pre_harness_backup"
            )
    finally:
        bind.exec_driver_sql("PRAGMA defer_foreign_keys = OFF")


def downgrade() -> None:
    tables = _tables()
    if "harness_run_dependencies" in tables:
        op.drop_index(
            "ix_harness_run_dependencies_resource",
            table_name="harness_run_dependencies",
        )
        op.drop_table("harness_run_dependencies")
    if "harness_definition_dependencies" in tables:
        op.drop_index(
            "ix_harness_definition_dependencies_resource",
            table_name="harness_definition_dependencies",
        )
        op.drop_table("harness_definition_dependencies")
    if "harness_principal_entitlements" in tables:
        op.drop_index(
            "ix_harness_principal_entitlements_member",
            table_name="harness_principal_entitlements",
        )
        op.drop_index(
            "ix_harness_principal_entitlements_fresh",
            table_name="harness_principal_entitlements",
        )
        op.drop_table("harness_principal_entitlements")

    _restore_pre_harness_resource_policies(tables)

    if "agent_runs" in tables:
        op.drop_index(
            "ix_agent_runs_project_created",
            table_name="agent_runs",
            if_exists=True,
        )
        for column in (
            "safe_error_code",
            "output_quarantined",
            "output_classification",
            "member_safe_json",
            "authorization_provenance_json",
            "project_id",
        ):
            if column in _columns("agent_runs"):
                op.drop_column("agent_runs", column)

    if "run_definitions" in tables:
        op.drop_index(
            "ix_run_definitions_project",
            table_name="run_definitions",
            if_exists=True,
        )
        for column in ("authorization_state", "project_id"):
            if column in _columns("run_definitions"):
                op.drop_column("run_definitions", column)
