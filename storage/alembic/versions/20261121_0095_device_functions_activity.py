"""Device switches, verified schedule claim attribution and 90-day lifecycle events."""

from alembic import op
import sqlalchemy as sa

revision = "20261121_0095"
down_revision = "20261120_0094"
branch_labels = None
depends_on = None
MIGRATION_SAFETY = "additive"
MIGRATION_COMPATIBILITY = "additive"


def upgrade() -> None:
    op.create_table(
        "device_health_links",
        sa.Column("mobile_id", sa.String(), primary_key=True),
        sa.Column("binding", sa.String()),
        sa.Column("subject", sa.String()),
        sa.Column("device_id", sa.String()),
        sa.Column("linked_at", sa.String()),
        sa.Column("ticket_binding", sa.String()),
        sa.Column("issuer_sid", sa.String()),
        sa.Column("ticket_hash", sa.String(), unique=True),
        sa.Column("code_hash", sa.String(), unique=True),
        sa.Column("code_expires_at", sa.Integer()),
        sa.Column("cleanup_pending", sa.Integer()),
        sa.Column("issued_at", sa.Integer()),
        sa.Column("expires_at", sa.Integer()),
        if_not_exists=True,
    )
    op.create_index("ix_dhl_device", "device_health_links", ["binding", "subject", "device_id"], if_not_exists=True)
    op.create_table(
        "device_activity_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("binding", sa.String(), nullable=False),
        sa.Column("subject", sa.String()),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String()),
        sa.Column("actor_subject", sa.String()),
        if_not_exists=True,
    )
    op.create_index("ix_dae_time", "device_activity_events", ["at"], if_not_exists=True)
    op.create_index("ix_dae_instance", "device_activity_events", ["binding", "at"], if_not_exists=True)
    op.create_index("ix_dae_person", "device_activity_events", ["binding", "subject", "at"], if_not_exists=True)
    op.create_index("ix_dae_device", "device_activity_events", ["binding", "device_id", "at"], if_not_exists=True)
    op.create_table(
        "device_function_settings",
        sa.Column("binding", sa.String(), primary_key=True),
        sa.Column("subject", sa.String(), primary_key=True),
        sa.Column("device_id", sa.String(), primary_key=True),
        sa.Column("schedule_on", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schedule_capable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notifications_on", sa.Integer(), nullable=False, server_default="1"),
        if_not_exists=True,
    )
    op.create_table(
        "device_session_extensions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("ticket_window", sa.Integer()),
        sa.Column("ticket_attempts", sa.Integer()),
        sa.Column("schedule_principal", sa.Text()),
        if_not_exists=True,
    )
    op.create_table(
        "device_schedule_links",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("registry_binding", sa.String()),
        sa.Column("registry_subject", sa.String()),
        sa.Column("registry_device", sa.String()),
        sa.Column("registry_activity_id", sa.Integer(), unique=True),
        sa.Column("schedule_probe_binding", sa.String()),
        if_not_exists=True,
    )
    op.create_index("ix_dsl_scope", "device_schedule_links", ["registry_binding", "registry_subject", "registry_device"], if_not_exists=True)


def downgrade() -> None:
    op.drop_table("device_schedule_links")
    op.drop_table("device_session_extensions")
    op.drop_table("device_health_links")
    op.drop_table("device_activity_events")
    op.drop_table("device_function_settings")
