from __future__ import annotations

import os
import sqlite3
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config

from config import paths
from storage.db import create_sqlite_engine, sqlite_url

INITIAL_REVISION = "20260501_0001"
LATEST_SCHEMA_REVISION = "20260622_0023"
REMOVE_LEGACY_DEFAULT_AGENT_REVISION = "20260530_0008"
ALLOW_DEV_STATE_MIGRATION_ENV = "AVIBE_ALLOW_DEV_STATE_MIGRATION"
INITIAL_TABLES = {
    "state_meta",
    "agents",
    "scopes",
    "scope_settings",
    "auth_codes",
    "agent_sessions",
    "runtime_records",
}


class UnsafeDefaultStateMigrationError(RuntimeError):
    """Raised when source-checkout code would migrate the user's default state."""


HEAD_TABLES = INITIAL_TABLES | {
    "run_definitions",
    "agent_runs",
    "show_pages",
    "messages",
    "agent_events",
    "show_session_events",
    "media_objects",
    "web_push_subscriptions",
}
PRE_SHOW_SESSION_EVENTS_HEAD_TABLES = INITIAL_TABLES | {
    "run_definitions",
    "agent_runs",
    "show_pages",
    "messages",
}
HEAD_REQUIRED_COLUMNS = {
    "agents": {"enabled"},
    "scope_settings": {"agent_name"},
    "agent_sessions": {"agent_id", "agent_name"},
    "messages": {"type", "source"},
    "run_definitions": {
        "deleted_at",
        "definition_type",
        "agent_name",
        "session_policy",
        "message",
        "message_payload_json",
        "last_run_id",
    },
    "agent_runs": {
        "definition_id",
        "source_kind",
        "source_actor",
        "parent_run_id",
        "agent_name",
        "agent_id",
        "agent_backend",
        "model",
        "reasoning_effort",
        "session_policy",
        "message",
        "message_payload_json",
        "result_text",
        "result_payload_json",
        "message_ids_json",
        "callback_session_id",
        "callback_status",
        "callback_error",
        "callback_run_id",
        "callback_completed_at",
        "cancel_requested",
        "cancel_requested_at",
    },
}
HEAD_ONLY_REQUIRED_COLUMNS = {
    "web_push_subscriptions": {"device_id"},
}
UNRELEASED_OLD_INITIAL_TABLES = [
    "session_messages",
    "chat_sessions",
    "channel_settings",
    "guild_settings",
    "guild_policies",
    "user_settings",
    "bind_codes",
    "agent_session_bindings",
    "active_threads",
    "active_polls",
    "processed_messages",
    "discovered_chats",
    "scopes",
    "schema_meta",
    "alembic_version",
]


def alembic_dir() -> Path:
    return Path(__file__).resolve().parent / "alembic"


def alembic_config(db_path: Path | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(alembic_dir()))
    cfg.set_main_option("sqlalchemy.url", sqlite_url(db_path or paths.get_sqlite_state_path()))
    return cfg


def run_migrations(db_path: Path | None = None, *, revision: str = "head") -> None:
    target_db = db_path or paths.get_sqlite_state_path()
    guard_source_checkout_default_state_migration(target_db)
    cfg = alembic_config(target_db)
    _reset_unreleased_initial_schema_drift(target_db)
    _repair_unreleased_head_schema_drift(target_db)
    _stamp_existing_initial_schema(target_db, cfg)
    command.upgrade(cfg, revision)


def background_tables_ready(db_path: Path | None = None) -> bool:
    target_db = (db_path or paths.get_sqlite_state_path()).expanduser().resolve()
    if not target_db.exists():
        return False
    with sqlite3.connect(target_db) as conn:
        tables = _table_names(conn)
        return _head_schema_ready(conn, tables)


def initialize_background_tables(db_path: Path | None = None) -> None:
    target_db = db_path or paths.get_sqlite_state_path()
    guard_source_checkout_default_state_migration(target_db)
    cfg = alembic_config(target_db)
    command.ensure_version(cfg)
    _repair_unreleased_head_schema_drift(target_db)
    _stamp_existing_initial_schema(target_db, cfg)
    command.upgrade(cfg, "head")


def guard_source_checkout_default_state_migration(db_path: Path) -> None:
    if _env_flag_enabled(ALLOW_DEV_STATE_MIGRATION_ENV):
        return
    if not _running_from_source_checkout():
        return
    if not _is_default_user_state_db(db_path):
        return

    source_root = Path(__file__).resolve().parents[1]
    raise UnsafeDefaultStateMigrationError(
        "Refusing to run SQLite migrations from an Avibe source checkout "
        f"against the default user state DB at {db_path.expanduser().resolve()}. "
        f"Source root: {source_root}. "
        "Set AVIBE_HOME to an isolated development directory before running "
        "worktree/source CLI commands. If you intentionally need to migrate the "
        f"default local state from source, set {ALLOW_DEV_STATE_MIGRATION_ENV}=1."
    )


def guard_source_checkout_default_state_bootstrap() -> None:
    guard_source_checkout_default_state_migration(paths.get_sqlite_state_path())


def _running_from_source_checkout() -> bool:
    source_root = Path(__file__).resolve().parents[1]
    return (source_root / "pyproject.toml").exists() and (source_root / "storage" / "migrations.py").exists()


def _is_default_user_state_db(db_path: Path) -> bool:
    target = db_path.expanduser().resolve()
    return target in _default_user_state_db_candidates()


def _default_user_state_db_candidates() -> set[Path]:
    home = Path.home().expanduser().resolve()
    return {
        (home / paths.AVIBE_HOME_DIRNAME / "state" / "vibe.sqlite").resolve(),
        (home / paths.LEGACY_HOME_DIRNAME / "state" / "vibe.sqlite").resolve(),
    }


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _reset_unreleased_initial_schema_drift(db_path: Path) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        if INITIAL_TABLES.issubset(tables):
            return
        if "alembic_version" not in tables:
            return
        version = conn.execute("select version_num from alembic_version").fetchone()
        if version != (INITIAL_REVISION,):
            return
        if not any(table in tables for table in UNRELEASED_OLD_INITIAL_TABLES):
            return

        conn.execute("PRAGMA foreign_keys = OFF")
        for table in UNRELEASED_OLD_INITIAL_TABLES:
            if table in tables:
                conn.execute(f'drop table if exists "{table}"')
        conn.commit()


def _repair_unreleased_head_schema_drift(db_path: Path) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        _rename_legacy_background_tables(conn, tables)
        tables = _table_names(conn)
        if not (tables <= {"alembic_version", "agents"}):
            _ensure_agents_table(conn, tables)
            tables = _table_names(conn)
        _repair_initial_required_columns(conn, tables)
        tables = _table_names(conn)
        if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
            return
        if "alembic_version" not in tables:
            return
        version = conn.execute("select version_num from alembic_version").fetchone()
        if version not in {("20260515_0002",), ("20260522_0003",), ("20260523_0004",)}:
            return

        if not _pre_show_session_events_head_schema_ready(conn, tables) and _repair_head_required_columns(conn, tables):
            conn.commit()


def _stamp_existing_initial_schema(db_path: Path, cfg: Config) -> None:
    path = db_path.expanduser().resolve()
    if not path.exists():
        return

    with sqlite3.connect(path) as conn:
        tables = _table_names(conn)
        _rename_legacy_background_tables(conn, tables)
        tables = _table_names(conn)
        if not (tables <= {"alembic_version"}):
            _ensure_agents_table(conn, tables)
            tables = _table_names(conn)
        _repair_initial_required_columns(conn, tables)
        tables = _table_names(conn)
        if not tables:
            return
        if not (tables - {"alembic_version", "agents"}):
            return
        if "alembic_version" in tables:
            version = conn.execute("select version_num from alembic_version").fetchone()
            if version is not None and version[0]:
                return
        missing_initial_tables = INITIAL_TABLES - tables
        if not (tables & (INITIAL_TABLES - {"agents"})) and (tables & {"run_definitions", "agent_runs"}):
            return
        if missing_initial_tables and (tables & INITIAL_TABLES):
            missing = ", ".join(sorted(missing_initial_tables))
            raise RuntimeError(f"existing SQLite schema is incomplete; missing initial tables: {missing}")
        if not INITIAL_TABLES.issubset(tables):
            return
        if HEAD_TABLES.issubset(tables):
            if not _head_schema_ready(conn, tables):
                _repair_head_required_columns(conn, tables)
                conn.commit()
                tables = _table_names(conn)
            if not _head_schema_ready(conn, tables):
                missing = _missing_head_schema_description(conn, tables)
                raise RuntimeError(f"existing SQLite head schema is incomplete; missing: {missing}")
            _ensure_head_indexes(conn, tables)
            _delete_historical_message_tool_calls(conn, tables)
            conn.commit()
            _run_remove_legacy_default_agent_migration(db_path)
            command.stamp(cfg, LATEST_SCHEMA_REVISION)
            _run_post_stamp_data_migrations(db_path)
            return
        if PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
            if not _pre_show_session_events_head_schema_ready(conn, tables):
                _repair_head_required_columns(conn, tables)
                conn.commit()
                tables = _table_names(conn)
            if not _pre_show_session_events_head_schema_ready(conn, tables):
                missing = _missing_pre_show_session_events_head_schema_description(conn, tables)
                raise RuntimeError(f"existing SQLite head schema is incomplete; missing: {missing}")
            _run_remove_legacy_default_agent_migration(db_path)
            command.stamp(cfg, REMOVE_LEGACY_DEFAULT_AGENT_REVISION)
            _run_post_stamp_data_migrations(db_path)
            return

    command.stamp(cfg, INITIAL_REVISION)


def _run_remove_legacy_default_agent_migration(db_path: Path) -> None:
    revision_module = import_module("storage.alembic.versions.20260530_0008_remove_legacy_default_agent")
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            original_op = revision_module.op
            revision_module.op = SimpleNamespace(get_bind=lambda: conn)
            try:
                revision_module.upgrade()
            finally:
                revision_module.op = original_op
    finally:
        engine.dispose()


def _run_post_stamp_data_migrations(db_path: Path) -> None:
    from storage.importer import _run_sqlite_data_migrations

    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            _run_sqlite_data_migrations(conn)
    finally:
        engine.dispose()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'table'",
        )
    }


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'pragma table_info("{table}")')}


def _head_schema_ready(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not HEAD_TABLES.issubset(tables):
        return False
    required = HEAD_REQUIRED_COLUMNS | HEAD_ONLY_REQUIRED_COLUMNS
    return all(required_columns.issubset(_column_names(conn, table)) for table, required_columns in required.items())


def _pre_show_session_events_head_schema_ready(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
        return False
    return all(required_columns.issubset(_column_names(conn, table)) for table, required_columns in HEAD_REQUIRED_COLUMNS.items())


def _repair_head_required_columns(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables):
        return False
    changed = False
    changed = _repair_initial_required_columns(conn, tables) or changed

    definition_columns = _column_names(conn, "run_definitions")
    if "definition_type" not in definition_columns and "task_type" in definition_columns:
        conn.execute('alter table "run_definitions" rename column "task_type" to "definition_type"')
        changed = True
        definition_columns = _column_names(conn, "run_definitions")
    for column, column_type in {
        "deleted_at": "VARCHAR",
        "agent_name": "VARCHAR",
        "session_policy": "VARCHAR",
        "message": "TEXT",
        "message_payload_json": "TEXT",
        "last_run_id": "VARCHAR",
    }.items():
        if column not in definition_columns:
            conn.execute(f'alter table "run_definitions" add column "{column}" {column_type}')
            changed = True
    definition_columns = _column_names(conn, "run_definitions")
    if "message" in definition_columns and "prompt" in definition_columns:
        conn.execute('update "run_definitions" set message = prompt where message is null')
    if "session_policy" in definition_columns:
        conn.execute(
            'update "run_definitions" set session_policy = '
            'case '
            'when session_id is not null and session_id != "" then "existing" '
            'when legacy_session_key is not null and legacy_session_key != "" then "existing" '
            "else null end "
            'where session_policy is null'
        )

    run_columns = _column_names(conn, "agent_runs")
    if "definition_id" not in run_columns and "task_id" in run_columns:
        conn.execute('alter table "agent_runs" rename column "task_id" to "definition_id"')
        changed = True
        run_columns = _column_names(conn, "agent_runs")
    for column, column_type in {
        "source_kind": "VARCHAR",
        "source_actor": "TEXT",
        "parent_run_id": "VARCHAR",
        "agent_name": "VARCHAR",
        "agent_id": "VARCHAR",
        "agent_backend": "VARCHAR",
        "model": "VARCHAR",
        "reasoning_effort": "VARCHAR",
        "session_policy": "VARCHAR",
        "message": "TEXT",
        "message_payload_json": "TEXT",
        "result_text": "TEXT",
        "result_payload_json": "TEXT",
        "message_ids_json": "TEXT",
        "callback_session_id": "VARCHAR",
        "callback_status": "VARCHAR",
        "callback_error": "TEXT",
        "callback_run_id": "VARCHAR",
        "callback_completed_at": "VARCHAR",
        "cancel_requested": "INTEGER not null default 0",
        "cancel_requested_at": "VARCHAR",
    }.items():
        if column not in run_columns:
            conn.execute(f'alter table "agent_runs" add column "{column}" {column_type}')
            changed = True
    run_columns = _column_names(conn, "agent_runs")
    if "message" in run_columns and "prompt" in run_columns:
        conn.execute('update "agent_runs" set message = prompt where message is null')

    # messages.type (20260531_0009): add + backfill, mirroring the migration, so
    # a drifted/unversioned head schema reaches readiness instead of leaving
    # messages_service.append writing a column that doesn't exist.
    if "messages" in tables and "type" not in _column_names(conn, "messages"):
        conn.execute('alter table "messages" add column "type" VARCHAR not null default \'assistant\'')
        conn.execute(
            """
            update messages set type = case
                when author = 'user' then 'user'
                when json_extract(content_json, '$.kind') = 'notify' then 'notify'
                when json_extract(content_json, '$.kind') = 'result' then 'result'
                when json_extract(content_json, '$.kind') in ('toolcall', 'tool_call') then 'tool_call'
                else 'assistant'
            end
            """
        )
        changed = True

    # messages.source (20260531_0010): origin (user/agent/harness), distinct
    # from author. Mirror the migration's add + backfill for drifted heads.
    if "messages" in tables and "source" not in _column_names(conn, "messages"):
        conn.execute('alter table "messages" add column "source" VARCHAR')
        conn.execute(
            """
            update messages set source = case
                when author = 'user' then 'user'
                when author = 'agent' then 'agent'
                when author = 'system' then 'agent'
                else null
            end
            """
        )
        changed = True

    if "web_push_subscriptions" in tables and "device_id" not in _column_names(conn, "web_push_subscriptions"):
        conn.execute('alter table "web_push_subscriptions" add column "device_id" VARCHAR')
        changed = True

    _ensure_head_indexes(conn, tables)
    return changed


def _rename_legacy_background_tables(conn: sqlite3.Connection, tables: set[str]) -> bool:
    changed = False
    if "background_tasks" in tables and "run_definitions" not in tables:
        conn.execute('alter table "background_tasks" rename to "run_definitions"')
        changed = True
    if "background_runs" in tables and "agent_runs" not in tables:
        conn.execute('alter table "background_runs" rename to "agent_runs"')
        changed = True
    return changed


def _ensure_agents_table(conn: sqlite3.Connection, tables: set[str]) -> bool:
    if "agents" in tables:
        return False
    conn.execute(
        """
        create table agents (
            id varchar primary key,
            name varchar not null,
            normalized_name varchar not null,
            description text,
            backend varchar not null,
            model varchar,
            reasoning_effort varchar,
            system_prompt text,
            enabled integer not null default 1,
            source varchar not null,
            source_ref text,
            metadata_json text not null,
            created_at varchar not null,
            updated_at varchar not null,
            constraint uq_agents_normalized_name unique (normalized_name)
        )
        """
    )
    conn.execute('create index if not exists ix_agents_backend on agents (backend)')
    conn.execute('create index if not exists ix_agents_updated on agents (updated_at)')
    return True


def _repair_initial_required_columns(conn: sqlite3.Connection, tables: set[str]) -> bool:
    changed = False
    if "agents" in tables:
        columns = _column_names(conn, "agents")
        if "enabled" not in columns:
            conn.execute('alter table "agents" add column "enabled" INTEGER not null default 1')
            changed = True
    if "scope_settings" in tables:
        columns = _column_names(conn, "scope_settings")
        if "agent_name" not in columns:
            conn.execute('alter table "scope_settings" add column "agent_name" VARCHAR')
            changed = True
    if "agent_sessions" in tables:
        columns = _column_names(conn, "agent_sessions")
        if "agent_id" not in columns:
            conn.execute('alter table "agent_sessions" add column "agent_id" VARCHAR')
            changed = True
        if "agent_name" not in columns:
            conn.execute('alter table "agent_sessions" add column "agent_name" VARCHAR')
            changed = True
    return changed


def _ensure_new_background_indexes(conn: sqlite3.Connection) -> None:
    conn.execute('create index if not exists ix_run_definitions_type_enabled on run_definitions (definition_type, enabled)')
    conn.execute('create index if not exists ix_run_definitions_session on run_definitions (session_id)')
    conn.execute('create index if not exists ix_run_definitions_agent on run_definitions (agent_name)')
    conn.execute('create index if not exists ix_run_definitions_updated on run_definitions (updated_at)')
    conn.execute('create index if not exists ix_agent_runs_definition_created on agent_runs (definition_id, created_at)')
    conn.execute('create index if not exists ix_agent_runs_status_created on agent_runs (status, created_at)')
    conn.execute('create index if not exists ix_agent_runs_type_status_created on agent_runs (run_type, status, created_at)')
    conn.execute('create index if not exists ix_agent_runs_session_created on agent_runs (session_id, created_at)')
    conn.execute('create index if not exists ix_agent_runs_agent_created on agent_runs (agent_name, created_at)')
    conn.execute('create index if not exists ix_agent_runs_callback_status on agent_runs (callback_status, completed_at)')


def _ensure_messages_query_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "agent_sessions" in tables:
        conn.execute(
            "create index if not exists ix_agent_sessions_scope_status_activity "
            "on agent_sessions (scope_id, status, last_active_at, created_at, id)"
        )
    if "messages" not in tables:
        return
    conn.execute('create index if not exists ix_messages_session_created_id on messages (session_id, created_at, id)')
    conn.execute('create index if not exists ix_messages_session_type_created_id on messages (session_id, type, created_at, id)')
    conn.execute('create index if not exists ix_messages_platform_session_created_id on messages (platform, session_id, created_at, id)')
    conn.execute(
        'create index if not exists ix_messages_unread_session '
        'on messages (platform, type, author, read_at, session_id)'
    )
    conn.execute(
        'create index if not exists ix_messages_mark_read '
        'on messages (session_id, author, read_at, created_at, id)'
    )
    conn.execute("drop index if exists ix_messages_inbox_activity")
    conn.execute(
        "create index ix_messages_inbox_activity "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )
    conn.execute(
        "create index if not exists ix_messages_inbox_agent_reply "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and type in ('result', 'notify', 'error')"
    )
    conn.execute("drop index if exists ix_messages_inbox_user_send")
    conn.execute(
        "create index ix_messages_inbox_user_send "
        "on messages (platform, session_id, created_at desc, id desc) "
        "where session_id is not null and author = 'user' and type not in ('queued', 'draft', 'pending', 'harness_dedupe')"
    )


def _ensure_agent_events_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "agent_events" not in tables:
        return
    conn.execute(
        "create index if not exists ix_agent_events_session_created_id "
        "on agent_events (session_id, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_session_type_created_id "
        "on agent_events (session_id, event_type, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_scope_created_id "
        "on agent_events (scope_id, created_at, id)"
    )
    conn.execute(
        "create index if not exists ix_agent_events_turn_sequence_id "
        "on agent_events (turn_id, sequence, id)"
    )


def _delete_historical_message_tool_calls(conn: sqlite3.Connection, tables: set[str]) -> None:
    if "messages" not in tables:
        return
    if "show_session_events" in tables:
        conn.execute(
            """
            update show_session_events
            set message_id = null
            where message_id in (select id from messages where type = 'tool_call')
            """
        )
    if "media_objects" in tables:
        conn.execute(
            """
            update media_objects
            set message_id = null
            where message_id in (select id from messages where type = 'tool_call')
            """
        )
    conn.execute("delete from messages where type = 'tool_call'")


def _ensure_head_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    if {"run_definitions", "agent_runs"}.issubset(tables):
        _ensure_new_background_indexes(conn)
    _ensure_messages_query_indexes(conn, tables)
    _ensure_agent_events_indexes(conn, tables)


def _missing_head_schema_description(conn: sqlite3.Connection, tables: set[str]) -> str:
    missing_parts = [f"tables {', '.join(sorted(HEAD_TABLES - tables))}"] if not HEAD_TABLES.issubset(tables) else []
    for table, required_columns in (HEAD_REQUIRED_COLUMNS | HEAD_ONLY_REQUIRED_COLUMNS).items():
        if table not in tables:
            continue
        missing_columns = required_columns - _column_names(conn, table)
        if missing_columns:
            missing_parts.append(f"{table}.{', '.join(sorted(missing_columns))}")
    return "; ".join(missing_parts) or "unknown head schema drift"


def _missing_pre_show_session_events_head_schema_description(conn: sqlite3.Connection, tables: set[str]) -> str:
    missing_parts = (
        [f"tables {', '.join(sorted(PRE_SHOW_SESSION_EVENTS_HEAD_TABLES - tables))}"]
        if not PRE_SHOW_SESSION_EVENTS_HEAD_TABLES.issubset(tables)
        else []
    )
    for table, required_columns in HEAD_REQUIRED_COLUMNS.items():
        if table not in tables:
            continue
        missing_columns = required_columns - _column_names(conn, table)
        if missing_columns:
            missing_parts.append(f"{table}.{', '.join(sorted(missing_columns))}")
    return "; ".join(missing_parts) or "unknown head schema drift"
