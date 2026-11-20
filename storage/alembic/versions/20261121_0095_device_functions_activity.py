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
        *[
            sa.Column(name, sa.String())
            for name in ("binding", "subject", "device_id", "linked_at", "ticket_binding", "issuer_sid")
        ],
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
    for key, cols in (
        ("time", ["at"]),
        ("instance", ["binding", "at"]),
        ("person", ["binding", "subject", "at"]),
        ("device", ["binding", "device_id", "at"]),
    ):
        op.create_index("ix_dae_" + key, "device_activity_events", cols, if_not_exists=True)
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
    for table, cols in (
        ("remote_device_sessions", ["ticket_window", "ticket_attempts"]),
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
    ):
        found = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
        for col in cols:
            if col not in found:
                op.add_column(
                    table,
                    sa.Column(
                        col,
                        sa.Integer()
                        if col
                        in {"ticket_window", "ticket_attempts", "registry_activity_id", "blocked_by_schedule_gate"}
                        else sa.String(),
                    ),
                )
    op.create_index(
        "uq_sso_registry_activity",
        "system_schedule_operations",
        ["registry_activity_id"],
        unique=True,
        if_not_exists=True,
    )
    op.create_index(
        "ix_sso_registry",
        "system_schedule_operations",
        ["registry_binding", "registry_subject", "created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("uq_sso_registry_activity", table_name="system_schedule_operations")
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
