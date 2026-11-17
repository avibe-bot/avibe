"""Add durable Agent-to-iOS system schedule operations and attempts.

Revision ID: 20261117_0090
Revises: 20261114_0089
"""

from alembic import op
import sqlalchemy as sa

revision = "20261117_0090"
down_revision = "20261114_0089"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "system_schedule_operations",
        sa.Column("operation_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("fire_at", sa.String(), nullable=False),
        sa.Column("time_zone", sa.String(), nullable=False),
        sa.Column("bound_device_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "idx_system_schedule_operations_session",
        "system_schedule_operations",
        ["session_id", "created_at"],
        if_not_exists=True,
    )
    op.create_table(
        "system_schedule_attempts",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("attempt_key", sa.String(), nullable=False, unique=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=True),
        sa.Column("lease_token", sa.String(), nullable=True, unique=True),
        sa.Column("claim_state", sa.String(), nullable=False),
        sa.Column("terminal_state", sa.String(), nullable=True),
        sa.Column("deadline", sa.String(), nullable=False),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "idx_system_schedule_attempts_expiry",
        "system_schedule_attempts",
        ["action", "claim_state", "terminal_state", "deadline"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_system_schedule_attempts_pending_claim",
        "system_schedule_attempts",
        ["action", "claim_state", "instance_id", "created_at", "request_id"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_system_schedule_attempts_sticky_claim",
        "system_schedule_attempts",
        [
            "action",
            "claim_state",
            "instance_id",
            "device_id",
            "updated_at",
            "request_id",
        ],
        sqlite_where=sa.text("terminal_state IS NULL AND receipt_json IS NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_system_schedule_attempts_operation",
        "system_schedule_attempts",
        ["operation_id", "action", "generation"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_system_schedule_attempts_operation",
        table_name="system_schedule_attempts",
    )
    op.drop_index(
        "idx_system_schedule_attempts_sticky_claim",
        table_name="system_schedule_attempts",
    )
    op.drop_index(
        "idx_system_schedule_attempts_pending_claim",
        table_name="system_schedule_attempts",
    )
    op.drop_index(
        "idx_system_schedule_attempts_expiry",
        table_name="system_schedule_attempts",
    )
    op.drop_table("system_schedule_attempts")
    op.drop_index(
        "idx_system_schedule_operations_session",
        table_name="system_schedule_operations",
    )
    op.drop_table("system_schedule_operations")
