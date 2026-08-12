"""index hidden agent starts and visible nonterminal outputs

Revision ID: 20260808_0048
Revises: 20260806_0047
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op

revision = "20260808_0048"
down_revision = "20260806_0047"
branch_labels = None
depends_on = None

_ORDER_EXPR = "coalesce(delivered_at, created_at)"

UPGRADE_ACTIVITY_PREDICATE = (
    "session_id is not null and type in "
    "('user', 'harness', 'agent_initiated', 'annotation', 'output', "
    "'result', 'notify', 'error', 'assistant')"
)
DOWNGRADE_ACTIVITY_PREDICATE = (
    "session_id is not null and type in "
    "('user', 'harness', 'annotation', 'result', 'notify', 'error', 'assistant')"
)
UPGRADE_USER_SEND_PREDICATE = (
    "session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness') "
    "or (author = 'harness' and type = 'agent_initiated') "
    "or (author = 'harness' and type = 'annotation'))"
)
UPGRADE_AGENT_REPLY_PREDICATE = (
    "session_id is not null and type in ('output', 'result', 'notify', 'error')"
)
DOWNGRADE_AGENT_REPLY_PREDICATE = (
    "session_id is not null and type in ('result', 'notify', 'error')"
)
DOWNGRADE_USER_SEND_PREDICATE = (
    "session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness') "
    "or (author = 'harness' and type = 'annotation'))"
)


def _replace_indexes(
    *,
    activity_predicate: str,
    agent_reply_predicate: str,
    user_send_predicate: str,
) -> None:
    bind = op.get_bind()
    for name in (
        "ix_messages_inbox_activity",
        "ix_messages_inbox_agent_reply",
        "ix_messages_inbox_user_send",
    ):
        bind.exec_driver_sql(f"drop index if exists {name}")
    bind.exec_driver_sql(
        "create index ix_messages_inbox_activity "
        f"on messages (platform, session_id, {_ORDER_EXPR} desc, id desc) "
        f"where {activity_predicate}"
    )
    bind.exec_driver_sql(
        "create index ix_messages_inbox_agent_reply "
        f"on messages (platform, session_id, {_ORDER_EXPR} desc, id desc) "
        f"where {agent_reply_predicate}"
    )
    bind.exec_driver_sql(
        "create index ix_messages_inbox_user_send "
        f"on messages (platform, session_id, {_ORDER_EXPR} desc, id desc) "
        f"where {user_send_predicate}"
    )


def upgrade() -> None:
    _replace_indexes(
        activity_predicate=UPGRADE_ACTIVITY_PREDICATE,
        agent_reply_predicate=UPGRADE_AGENT_REPLY_PREDICATE,
        user_send_predicate=UPGRADE_USER_SEND_PREDICATE,
    )


def downgrade() -> None:
    _replace_indexes(
        activity_predicate=DOWNGRADE_ACTIVITY_PREDICATE,
        agent_reply_predicate=DOWNGRADE_AGENT_REPLY_PREDICATE,
        user_send_predicate=DOWNGRADE_USER_SEND_PREDICATE,
    )
