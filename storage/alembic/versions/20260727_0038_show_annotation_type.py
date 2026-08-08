"""add the Show annotation message type's inbox index support

This revision changes schema only. Rows written before it keep their original
types and are not supported by the annotation renderer. The feature never
shipped, so no installation holds annotation history that requires migration.

Revision ID: 20260727_0038
Revises: 20260726_0037
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op

revision = "20260727_0038"
down_revision = "20260726_0037"
branch_labels = None
depends_on = None

UPGRADE_USER_SEND_PREDICATE = (
    "session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness') "
    "or (author = 'harness' and type = 'annotation'))"
)
DOWNGRADE_USER_SEND_PREDICATE = (
    "session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness'))"
)

_CREATE_USER_SEND_INDEX_PREFIX = (
    "create index ix_messages_inbox_user_send "
    "on messages (platform, session_id, created_at desc, id desc) where "
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        _CREATE_USER_SEND_INDEX_PREFIX + UPGRADE_USER_SEND_PREDICATE
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        _CREATE_USER_SEND_INDEX_PREFIX + DOWNGRADE_USER_SEND_PREDICATE
    )
