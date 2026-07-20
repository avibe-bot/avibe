"""exclude silent completion markers from the inbox activity index

The invisible ``silent`` completion marker (messages_service.SILENT_TYPE) is added to
``NON_CONVERSATION_TYPES``, so the inbox ``conversation_only`` subqueries now filter
``type NOT IN (..., 'silent')``. The partial index ``ix_messages_inbox_activity`` must
carry the same predicate, or SQLite stops using it for those top-1 probes (a stricter
``NOT IN`` predicate does not match the looser partial index) and large inbox refreshes
fall back to scans.

Revision ID: 20260721_0031
Revises: 20260716_0030
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op

revision = "20260721_0031"
down_revision = "20260716_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_activity")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_activity "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and "
        "type not in ('queued', 'draft', 'pending', 'harness_dedupe', 'silent')"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_activity")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_activity "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and "
        "type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )
