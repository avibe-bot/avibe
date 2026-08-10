"""Index transcript-visible Vault waiter outcomes without settling inbox replies.

Revision ID: 20260809_0049
Revises: 20260808_0048
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op

revision = "20260809_0049"
down_revision = "20260808_0048"
branch_labels = None
depends_on = None

_ORDER_EXPR = "coalesce(delivered_at, created_at)"

UPGRADE_ACTIVITY_PREDICATE = (
    "session_id is not null and type in "
    "('user', 'harness', 'agent_initiated', 'annotation', 'output', "
    "'result', 'notify', 'vault', 'error', 'assistant')"
)
DOWNGRADE_ACTIVITY_PREDICATE = (
    "session_id is not null and type in "
    "('user', 'harness', 'agent_initiated', 'annotation', 'output', "
    "'result', 'notify', 'error', 'assistant')"
)
UPGRADE_USER_SEND_PREDICATE = (
    "session_id is not null and ((author = 'user' and type = 'user') "
    "or (author = 'harness' and type = 'harness') "
    "or (author = 'harness' and type = 'agent_initiated') "
    "or (author = 'harness' and type = 'annotation'))"
)
DOWNGRADE_USER_SEND_PREDICATE = UPGRADE_USER_SEND_PREDICATE
UPGRADE_AGENT_REPLY_PREDICATE = (
    "session_id is not null and type in ('output', 'result', 'notify', 'vault', 'error')"
)
DOWNGRADE_AGENT_REPLY_PREDICATE = (
    "session_id is not null and type in ('output', 'result', 'notify', 'error')"
)
_LEGACY_WAITER_PREDICATE = (
    "type = 'notify' and json_valid(metadata_json) = 1 "
    "and json_extract(metadata_json, '$.source_kind') = 'callback' "
    "and json_extract(metadata_json, '$.source_actor') like 'vault:%'"
)
_VAULT_WAITER_PREDICATE = _LEGACY_WAITER_PREDICATE.replace("type = 'notify'", "type = 'vault'")


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
    bind = op.get_bind()
    bind.exec_driver_sql(
        "update messages set type = 'vault' where "
        f"{_LEGACY_WAITER_PREDICATE}"
    )
    _replace_indexes(
        activity_predicate=UPGRADE_ACTIVITY_PREDICATE,
        agent_reply_predicate=UPGRADE_AGENT_REPLY_PREDICATE,
        user_send_predicate=UPGRADE_USER_SEND_PREDICATE,
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "update messages set type = 'notify' where "
        f"{_VAULT_WAITER_PREDICATE}"
    )
    _replace_indexes(
        activity_predicate=DOWNGRADE_ACTIVITY_PREDICATE,
        agent_reply_predicate=DOWNGRADE_AGENT_REPLY_PREDICATE,
        user_send_predicate=DOWNGRADE_USER_SEND_PREDICATE,
    )
