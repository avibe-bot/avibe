"""decouple session visibility from scope placement

Revision ID: 20260723_0032
Revises: 20260721_0031
Create Date: 2026-07-23
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "20260723_0032"
down_revision = "20260721_0031"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {
        str(row[0])
        for row in bind.exec_driver_sql(
            "select name from sqlite_master where type = 'table'"
        )
    }


def _columns(bind, table: str) -> dict[str, tuple]:
    return {
        str(row[1]): tuple(row)
        for row in bind.exec_driver_sql(f'pragma table_info("{table}")').fetchall()
    }


def _indexes(bind, table: str) -> set[str]:
    return {
        str(row[1])
        for row in bind.exec_driver_sql(f'pragma index_list("{table}")').fetchall()
    }


def _caller_session_id(run, known_session_ids: set[str]) -> str | None:
    source_actor = str(run.source_actor or "").strip()
    if source_actor in known_session_ids:
        return source_actor
    try:
        metadata = json.loads(run.metadata_json or "{}")
    except (TypeError, ValueError):
        return None
    caller = metadata.get("caller_context") if isinstance(metadata, dict) else None
    if not isinstance(caller, dict):
        return None
    return str(caller.get("session_id") or "").strip() or None


def _backfill_legacy_sessions(bind) -> None:
    legacy_ids = {
        str(row[0])
        for row in bind.exec_driver_sql(
            """
            select s.id
            from agent_sessions as s
            join scopes as sc on sc.id = s.scope_id
            where sc.native_type = 'private_agent_run'
            """
        ).fetchall()
    }
    if not legacy_ids:
        return

    rows = bind.exec_driver_sql("select id, scope_id from agent_sessions").fetchall()
    original_scope = {str(row[0]): row[1] for row in rows}
    runs = bind.exec_driver_sql(
        """
        select session_id, source_actor, metadata_json
        from agent_runs
        where session_id is not null
        order by created_at desc, id desc
        """
    ).fetchall()
    caller_by_session: dict[str, str] = {}
    for run in runs:
        spawned_id = str(run.session_id or "").strip()
        if spawned_id in caller_by_session:
            continue
        caller_id = _caller_session_id(run, set(original_scope))
        if caller_id in original_scope:
            caller_by_session[spawned_id] = caller_id

    def resolved_scope(session_id: str, seen: set[str]) -> str | None:
        if session_id in seen:
            return None
        scope_id = original_scope.get(session_id)
        if session_id not in legacy_ids:
            return str(scope_id) if scope_id else None
        caller_id = caller_by_session.get(session_id)
        if not caller_id:
            return None
        return resolved_scope(caller_id, {*seen, session_id})

    # Self-anchoring before the scope move preserves the unique
    # (scope_id, session_anchor) invariant even when siblings converge.
    for session_id in legacy_ids:
        bind.exec_driver_sql(
            """
            update agent_sessions
            set session_anchor = ?, scope_id = ?, visibility = 'background'
            where id = ?
            """,
            (session_id, resolved_scope(session_id, set()), session_id),
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "agent_sessions" not in tables:
        return

    if "visibility" not in _columns(bind, "agent_sessions"):
        op.add_column(
            "agent_sessions",
            sa.Column("visibility", sa.String(), nullable=False, server_default="foreground"),
        )
    if "ix_agent_sessions_visibility" not in _indexes(bind, "agent_sessions"):
        op.create_index("ix_agent_sessions_visibility", "agent_sessions", ["visibility"])

    message_refs: dict[str, list[tuple[str, str]]] = {}
    for table, key_column in (
        ("show_session_events", "id"),
        ("media_objects", "token"),
    ):
        if table in tables:
            message_refs[table] = [
                (str(row[0]), str(row[1]))
                for row in bind.exec_driver_sql(
                    f"select {key_column}, message_id from {table} where message_id is not null"
                ).fetchall()
            ]

    if "messages" in tables and _columns(bind, "messages")["scope_id"][3]:
        with op.batch_alter_table("messages") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=True)
        existing_message_ids = {
            str(row[0])
            for row in bind.exec_driver_sql("select id from messages").fetchall()
        }
        for table, key_column in (
            ("show_session_events", "id"),
            ("media_objects", "token"),
        ):
            for row_key, message_id in message_refs.get(table, []):
                if message_id in existing_message_ids:
                    bind.exec_driver_sql(
                        f"update {table} set message_id = ? where {key_column} = ?",
                        (message_id, row_key),
                    )
    if "agent_events" in tables and _columns(bind, "agent_events")["scope_id"][3]:
        with op.batch_alter_table("agent_events") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=True)
    if "media_objects" in tables and _columns(bind, "media_objects")["scope_id"][3]:
        with op.batch_alter_table("media_objects") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=True)

    _backfill_legacy_sessions(bind)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)
    if "messages" in tables:
        bind.exec_driver_sql("delete from messages where scope_id is null")
        with op.batch_alter_table("messages") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=False)
    if "agent_events" in tables:
        bind.exec_driver_sql("delete from agent_events where scope_id is null")
        with op.batch_alter_table("agent_events") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=False)
    if "media_objects" in tables:
        bind.exec_driver_sql("delete from media_objects where scope_id is null")
        with op.batch_alter_table("media_objects") as batch_op:
            batch_op.alter_column("scope_id", existing_type=sa.String(), nullable=False)
    op.drop_index("ix_agent_sessions_visibility", table_name="agent_sessions")
    op.drop_column("agent_sessions", "visibility")
