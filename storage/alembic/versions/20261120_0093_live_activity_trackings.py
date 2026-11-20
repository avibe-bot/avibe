"""Persist one Turn's native Live Activity tracking."""

from alembic import op
import sqlalchemy as sa

revision = "20261120_0093"
down_revision = "20261120_0092"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "live_activity_trackings",
        sa.Column("activity_id", sa.String(), primary_key=True),
        sa.Column("owner_user_key", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("turn_id", sa.String(), nullable=False),
        sa.Column("push_registration_id", sa.String(), nullable=False),
        sa.Column("activity_token", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_phase", sa.String(), nullable=False),
        sa.Column("stop_reason", sa.String()),
        sa.Column("last_timestamp", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("ended_at", sa.Integer()),
        sa.Column("authorization_json", sa.Text()),
        sa.Column("pending_state", sa.Text()),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.Integer(), nullable=False, server_default="0"),
        if_not_exists=True,
    )
    op.create_index("ix_live_activity_status", "live_activity_trackings", ["status", "expires_at"], if_not_exists=True)
    op.create_index(
        "ix_live_activity_registration", "live_activity_trackings", ["push_registration_id", "status"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("live_activity_trackings")
