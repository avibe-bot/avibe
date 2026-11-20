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
    op.add_column("remote_device_sessions", sa.Column("ticket_window", sa.Integer(), nullable=True))
    op.add_column("remote_device_sessions", sa.Column("ticket_attempts", sa.Integer(), nullable=True))
    op.add_column("system_schedule_operations", sa.Column("registry_binding", sa.String(), nullable=True))
    op.add_column("system_schedule_operations", sa.Column("registry_subject", sa.String(), nullable=True))
    op.add_column("system_schedule_operations", sa.Column("registry_device", sa.String(), nullable=True))
    op.add_column("system_schedule_operations", sa.Column("registry_activity_id", sa.Integer(), nullable=True))
    op.add_column("system_schedule_operations", sa.Column("blocked_by_schedule_gate", sa.Integer(), nullable=True))
    op.create_index("ix_sso_registry_activity", "system_schedule_operations", ["registry_activity_id"], if_not_exists=True)
    op.create_index(
        "ix_sso_registry",
        "system_schedule_operations",
        ["registry_binding", "registry_subject", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_sso_registry_activity", table_name="system_schedule_operations")
    op.drop_index("ix_sso_registry", table_name="system_schedule_operations")
    for table, cols in (
        (
            "system_schedule_operations",
            [
                "registry_binding",
                "registry_subject",
                "registry_device",
                "registry_activity_id",
                "blocked_by_schedule_gate",
            ],
        ),
        ("remote_device_sessions", ["ticket_window", "ticket_attempts"]),
    ):
        for col in cols:
            op.drop_column(table, col)
    op.drop_table("device_health_links")
    op.drop_table("device_activity_events")
    op.drop_table("device_function_settings")
