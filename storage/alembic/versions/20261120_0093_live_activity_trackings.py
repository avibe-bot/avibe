"""Persist one Turn's native Live Activity tracking."""

from alembic import op
import sqlalchemy as sa

revision = "20261120_0093"
down_revision = "20261118_0091"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "live_activity_trackings",
        sa.Column("activity_id", sa.String(), primary_key=True),
        *[
            sa.Column(name, sa.String(), nullable=False)
            for name in (
                "owner_user_key",
                "session_id",
                "turn_id",
                "push_registration_id",
                "activity_token",
                "status",
                "last_phase",
            )
        ],
        sa.Column("stop_reason", sa.String()),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "last_timestamp",
                "confirmed_at",
                "requested_at",
                "created_at",
                "updated_at",
                "expires_at",
                "started_at",
            )
        ],
        sa.Column("ended_at", sa.Integer()),
        sa.Column("authorization_json", sa.Text()),
        sa.Column("pending_state", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("status in ('tracking', 'ended', 'stopped')", name="ck_live_activity_status"),
    )
    op.create_index("ix_live_activity_status", "live_activity_trackings", ["status", "expires_at"])
    op.create_index("ix_live_activity_registration", "live_activity_trackings", ["push_registration_id", "status"])


def downgrade() -> None:
    op.drop_table("live_activity_trackings")
