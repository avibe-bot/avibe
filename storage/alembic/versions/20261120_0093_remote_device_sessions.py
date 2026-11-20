"""Register browser sessions without rewriting existing cookies or account rows."""

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
        "remote_device_deadlines",
        sa.Column("key", sa.String(), primary_key=True, nullable=False),
        sa.Column("issued_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "ix_web_push_subscriptions_device_enabled",
        "web_push_subscriptions",
        ["user_key", "device_id", "enabled"],
        if_not_exists=True,
    )
    op.create_index("ix_rdd_expiry", "remote_device_deadlines", ["expires_at"], if_not_exists=True)
    op.create_table(
        "remote_device_sessions",
        sa.Column("session_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("binding", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("login_role", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("identity_source", sa.String(), nullable=False),
        sa.Column("push_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("app_version", sa.String(), nullable=True),
        sa.Column("system_version", sa.String(), nullable=True),
        sa.Column("signed_in_at", sa.Integer(), nullable=False),
        sa.Column("last_active_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("path_kind", sa.String(), nullable=True),
        sa.Column("path_family", sa.String(), nullable=True),
        sa.Column("path_rtt_ms", sa.Integer(), nullable=True),
        if_not_exists=True,
    )
    op.create_index("ix_rds_binding_expiry", "remote_device_sessions", ["binding", "expires_at"], if_not_exists=True)
    op.create_index(
        "ix_rds_subject_expiry", "remote_device_sessions", ["binding", "subject", "expires_at"], if_not_exists=True
    )
    op.create_index(
        "ix_rds_device_expiry", "remote_device_sessions", ["binding", "device_id", "expires_at"], if_not_exists=True
    )
    op.create_index("ix_rds_expiry", "remote_device_sessions", ["expires_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_web_push_subscriptions_device_enabled", table_name="web_push_subscriptions")
    op.drop_table("remote_device_sessions")
    op.drop_table("remote_device_deadlines")
