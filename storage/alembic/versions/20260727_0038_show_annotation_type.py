"""add the Show annotation message type

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
_LEGACY_REVERSE_MARK_EVENT_TYPES = (
    "'assistant.mark.created', 'assistant.mark.resolved'"
)
_ALL_REVERSE_MARK_EVENT_TYPES = (
    "'assistant.mark.created', 'assistant.mark.updated', 'assistant.mark.resolved'"
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        _CREATE_USER_SEND_INDEX_PREFIX + UPGRADE_USER_SEND_PREDICATE
    )
    bind.exec_driver_sql(
        "update messages "
        "set type = 'annotation', "
        "content_json = case "
        "when json_valid(content_json) then "
        "case when json_type(content_json) = 'object' then "
        "json_set(content_json, '$.annotation', "
        "json_object('direction', 'agent', 'action', "
        "case json_extract(metadata_json, '$.show_event_type') "
        "when 'assistant.mark.resolved' then 'resolved' else 'created' end)) "
        "else json_object("
        "'text', coalesce(content_text, ''), "
        "'annotation', json_object('direction', 'agent', 'action', "
        "case json_extract(metadata_json, '$.show_event_type') "
        "when 'assistant.mark.resolved' then 'resolved' else 'created' end)) end "
        "else json_object("
        "'text', coalesce(content_text, ''), "
        "'annotation', json_object('direction', 'agent', 'action', "
        "case json_extract(metadata_json, '$.show_event_type') "
        "when 'assistant.mark.resolved' then 'resolved' else 'created' end)) end "
        "where type = 'assistant' "
        "and case when json_valid(metadata_json) "
        "then json_extract(metadata_json, '$.show_event_type') end "
        f"in ({_LEGACY_REVERSE_MARK_EVENT_TYPES})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        "update messages "
        "set type = 'assistant', "
        "content_json = case "
        "when json_valid(content_json) then "
        "case when json_type(content_json) = 'object' "
        "then json_remove(content_json, '$.annotation') "
        "else content_json end "
        "else content_json end "
        "where type = 'annotation' "
        "and case when json_valid(metadata_json) "
        "then json_extract(metadata_json, '$.show_event_type') end "
        f"in ({_ALL_REVERSE_MARK_EVENT_TYPES})"
    )
    bind.exec_driver_sql(
        "update messages "
        "set type = case "
        "when author = 'harness' and source = 'harness' "
        "and author_name = 'show_annotation' then 'harness' "
        "else 'user' end, "
        "content_json = case "
        "when json_valid(content_json) then "
        "case when json_type(content_json) = 'object' "
        "then json_remove(content_json, '$.annotation') "
        "else content_json end "
        "else content_json end "
        "where type = 'annotation' "
        "and case when json_valid(metadata_json) "
        "then json_extract(metadata_json, '$.show_event_type') end "
        "= 'human.annotation.created'"
    )
    bind.exec_driver_sql("drop index if exists ix_messages_inbox_user_send")
    bind.exec_driver_sql(
        _CREATE_USER_SEND_INDEX_PREFIX + DOWNGRADE_USER_SEND_PREDICATE
    )
