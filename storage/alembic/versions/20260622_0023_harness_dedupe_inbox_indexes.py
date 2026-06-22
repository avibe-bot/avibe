"""exclude harness dedupe rows from inbox partial indexes

Revision ID: 20260622_0023
Revises: 20260610_0022
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op

revision = "20260622_0023"
down_revision = "20260610_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_activity")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_activity "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_user_send "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and author = 'user' and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_user_send "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and author = 'user' and type not in ('queued', 'draft', 'pending')"
    )
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_activity")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_activity "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and type not in ('queued', 'draft', 'pending')"
    )
