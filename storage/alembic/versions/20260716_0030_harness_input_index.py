"""index human and harness inbox inputs

Revision ID: 20260716_0030
Revises: 20260707_0029
Create Date: 2026-07-16
"""

from __future__ import annotations

from alembic import op

revision = "20260716_0030"
down_revision = "20260707_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_user_send "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and "
        "((author = 'user' and type = 'user') or "
        "(author = 'harness' and type = 'harness'))"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_user_send "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and author = 'user' "
        "and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )
