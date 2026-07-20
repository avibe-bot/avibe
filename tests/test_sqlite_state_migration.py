from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from config import paths
from config.v2_settings import ChannelSettings, RoutingSettings, SettingsState, SettingsStore
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.importer import JSON_IMPORT_MARKER, ensure_sqlite_state
from storage import migrations
from storage.migrations import UnsafeDefaultStateMigrationError, background_tables_ready, run_migrations
from storage.models import metadata
from storage.settings_service import SQLiteSettingsService


HEAD_REVISION = "20260721_0031"


def _index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("select sql from sqlite_master where type = 'index' and name = ?", (name,)).fetchone()
    assert row is not None
    return str(row[0] or "")


def test_run_migrations_creates_initial_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'",
            )
        }
        assert "alembic_version" in tables
        assert "scope_settings" in tables
        assert "agent_sessions" in tables
        assert "runtime_records" in tables
        assert "run_definitions" in tables
        assert "agent_runs" in tables
        assert "show_pages" in tables
        assert "show_session_events" in tables
        assert "agent_events" in tables
        assert "media_objects" in tables
        assert "web_push_subscriptions" in tables
        assert "vault_auth_factors" in tables
        assert "vault_operation_challenges" in tables
        agent_event_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('agent_events')",
            )
        }
        assert "ix_agent_events_session_created_id" in agent_event_indexes
        assert "ix_agent_events_session_type_created_id" in agent_event_indexes
        assert "ix_agent_events_scope_created_id" in agent_event_indexes
        assert "ix_agent_events_turn_sequence_id" in agent_event_indexes
        message_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('messages')",
            )
        }
        vault_secret_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('vault_secrets')",
            )
        }
        vault_request_triggers = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'trigger' and tbl_name = 'vault_requests'",
            )
        }
        vault_auth_factor_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('vault_auth_factors')",
            )
        }
        vault_challenge_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('vault_operation_challenges')",
            )
        }
        assert "ix_messages_session_created_id" in message_indexes
        assert "ix_messages_session_type_created_id" in message_indexes
        assert "ix_messages_platform_session_created_id" in message_indexes
        assert "ix_messages_unread_session" in message_indexes
        assert "ix_messages_mark_read" in message_indexes
        assert "ix_messages_inbox_activity" in message_indexes
        assert "ix_messages_inbox_agent_reply" in message_indexes
        assert "ix_messages_inbox_user_send" in message_indexes
        assert "harness_dedupe" in _index_sql(conn, "ix_messages_inbox_activity")
        assert "author = 'harness'" in _index_sql(conn, "ix_messages_inbox_user_send")
        assert "uq_vault_secrets_name_folded" in vault_secret_indexes
        assert "lower(name)" in _index_sql(conn, "uq_vault_secrets_name_folded").lower()
        assert "ix_vault_auth_factors_kind_rp" in vault_auth_factor_indexes
        assert "ix_vault_operation_challenges_lookup" in vault_challenge_indexes
        assert "ix_vault_operation_challenges_consumed" in vault_challenge_indexes
        assert "trg_vault_requests_pending_provision_name_case_insert" in vault_request_triggers
        assert "trg_vault_requests_pending_provision_name_case_update" in vault_request_triggers
        agent_session_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('agent_sessions')",
            )
        }
        assert "ix_agent_sessions_scope_status_activity" in agent_session_indexes
        media_columns = {
            row[1] for row in conn.execute("pragma table_info(media_objects)")
        }
        assert "mtime_ns" in media_columns  # 20260603_0014: dedup fingerprint
        assert "width_px" in media_columns  # 20260604_0015: zero-shift image box
        assert "height_px" in media_columns
        background_columns = {
            row[1]
            for row in conn.execute(
                "pragma table_info(run_definitions)",
            )
        }
        assert "deleted_at" in background_columns
        version = conn.execute("select version_num from alembic_version").fetchone()
        assert version == (HEAD_REVISION,)


def test_run_migrations_serializes_alembic_context(monkeypatch, tmp_path: Path) -> None:
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    calls: list[Path] = []

    def fake_run_locked(
        target_db: Path,
        *,
        revision: str,
        prune_backups_after_upgrade: bool,
    ) -> None:
        assert revision == "head"
        assert prune_backups_after_upgrade is True
        calls.append(target_db)
        if len(calls) == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()

    monkeypatch.setattr(migrations, "_run_migrations_locked", fake_run_locked)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_migrations, tmp_path / "first.sqlite")
        assert first_entered.wait(2)
        second = pool.submit(run_migrations, tmp_path / "second.sqlite")
        try:
            assert not second_entered.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_entered.is_set()
    assert calls == [
        (tmp_path / "first.sqlite").resolve(),
        (tmp_path / "second.sqlite").resolve(),
    ]


def test_run_migrations_blocks_source_checkout_default_user_state(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    db_path = home / ".avibe" / "state" / "vibe.sqlite"

    monkeypatch.setattr(migrations.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(paths.AVIBE_HOME_ENV, raising=False)
    monkeypatch.delenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, raising=False)
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)

    with pytest.raises(UnsafeDefaultStateMigrationError) as exc:
        run_migrations(db_path)

    message = str(exc.value)
    assert "Refusing to run SQLite migrations from an Avibe source checkout" in message
    assert str(db_path) in message
    assert not db_path.exists()


def test_run_migrations_blocks_default_state_when_override_is_falsey(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    db_path = home / ".avibe" / "state" / "vibe.sqlite"

    monkeypatch.setattr(migrations.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(paths.AVIBE_HOME_ENV, raising=False)
    monkeypatch.setenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, "0")
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        run_migrations(db_path)

    assert not db_path.exists()


def test_ensure_sqlite_state_blocks_source_checkout_default_user_state_before_dirs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db_path = home / ".avibe" / "state" / "vibe.sqlite"

    monkeypatch.setattr(migrations.Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(paths.AVIBE_HOME_ENV, raising=False)
    monkeypatch.delenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, raising=False)
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        ensure_sqlite_state()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_ensure_sqlite_state_blocks_explicit_avibe_home_pointing_at_default_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    avibe_home = home / ".avibe"
    db_path = avibe_home / "state" / "vibe.sqlite"

    monkeypatch.setattr(migrations.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(paths.AVIBE_HOME_ENV, str(avibe_home))
    monkeypatch.delenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, raising=False)
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)

    with pytest.raises(UnsafeDefaultStateMigrationError):
        ensure_sqlite_state()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_run_migrations_allows_source_checkout_with_explicit_avibe_home(monkeypatch, tmp_path: Path) -> None:
    avibe_home = tmp_path / "dev-home"
    db_path = avibe_home / "state" / "vibe.sqlite"

    monkeypatch.setenv(paths.AVIBE_HOME_ENV, str(avibe_home))
    monkeypatch.delenv(migrations.ALLOW_DEV_STATE_MIGRATION_ENV, raising=False)
    monkeypatch.setattr(migrations, "_running_from_source_checkout", lambda: True)

    db_path.parent.mkdir(parents=True)
    run_migrations()

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert version == (HEAD_REVISION,)


def test_initial_migration_is_schema_snapshot() -> None:
    migration_path = Path("storage/alembic/versions/20260501_0001_initial_sqlite_state.py")

    source = migration_path.read_text(encoding="utf-8")

    assert "from storage.models" not in source
    assert "metadata.create_all" not in source


def test_alembic_env_sets_wal_before_transaction() -> None:
    env_path = Path("storage/alembic/env.py")

    source = env_path.read_text(encoding="utf-8")

    assert "with connectable.begin()" not in source
    assert "with connectable.connect()" in source
    assert "PRAGMA journal_mode = WAL" in source


def test_run_migrations_stamps_existing_initial_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert version == (HEAD_REVISION,)


def test_run_migrations_repairs_head_indexes_before_stamping_head(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute("drop index if exists ix_agent_sessions_scope_status_activity")
        conn.execute("drop index if exists ix_messages_session_created_id")
        conn.execute("drop index if exists ix_messages_session_type_created_id")
        conn.execute("drop index if exists ix_messages_platform_session_created_id")
        conn.execute("drop index if exists ix_messages_unread_session")
        conn.execute("drop index if exists ix_messages_mark_read")
        conn.execute("drop index if exists ix_messages_inbox_activity")
        conn.execute("drop index if exists ix_messages_inbox_agent_reply")
        conn.execute("drop index if exists ix_messages_inbox_user_send")
        conn.commit()
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        message_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('messages')",
            )
        }
        agent_session_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('agent_sessions')",
            )
        }
    assert version == (HEAD_REVISION,)
    assert "ix_messages_session_created_id" in message_indexes
    assert "ix_messages_session_type_created_id" in message_indexes
    assert "ix_messages_platform_session_created_id" in message_indexes
    assert "ix_messages_unread_session" in message_indexes
    assert "ix_messages_mark_read" in message_indexes
    assert "ix_messages_inbox_activity" in message_indexes
    assert "ix_messages_inbox_agent_reply" in message_indexes
    assert "ix_messages_inbox_user_send" in message_indexes
    with sqlite3.connect(db_path) as conn:
        assert "harness_dedupe" in _index_sql(conn, "ix_messages_inbox_activity")
        assert "author = 'harness'" in _index_sql(conn, "ix_messages_inbox_user_send")
    assert "ix_agent_sessions_scope_status_activity" in agent_session_indexes


def test_run_migrations_adds_agent_events_from_previous_head(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260606_0019")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select name from sqlite_master where name = 'agent_events'").fetchone() is None
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert version == ("20260606_0019",)

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        agent_event_indexes = {
            row[1]
            for row in conn.execute(
                "select seq, name from pragma_index_list('agent_events')",
            )
        }
    assert version == (HEAD_REVISION,)
    assert "ix_agent_events_session_created_id" in agent_event_indexes
    assert "ix_agent_events_session_type_created_id" in agent_event_indexes
    assert "ix_agent_events_scope_created_id" in agent_event_indexes
    assert "ix_agent_events_turn_sequence_id" in agent_event_indexes


def test_run_migrations_rebuilds_inbox_indexes_for_harness_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260610_0022")
    with sqlite3.connect(db_path) as conn:
        assert "harness_dedupe" not in _index_sql(conn, "ix_messages_inbox_activity")
        assert "harness_dedupe" not in _index_sql(conn, "ix_messages_inbox_user_send")

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        assert version == (HEAD_REVISION,)
        assert "harness_dedupe" in _index_sql(conn, "ix_messages_inbox_activity")
        assert "author = 'harness'" in _index_sql(conn, "ix_messages_inbox_user_send")


def test_run_migrations_backfills_legacy_harness_prompt_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260707_0029")
    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "scope_harness")
        _insert_agent_session(
            conn,
            row_id="ses_harness",
            scope_id="scope_harness",
            anchor="ses_harness",
            workdir=None,
            backend="codex",
            native="",
            last_active="2026-07-15T00:00:00Z",
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, source,
                content_text, content_json, metadata_json, created_at, updated_at
            ) values (
                'msg_harness', 'scope_harness', 'ses_harness', 'avibe',
                'user', 'user', 'harness', 'scheduled input', '{}', '{}',
                '2026-07-15T00:00:00Z', '2026-07-15T00:00:00Z'
            )
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select author, type, source from messages where id = 'msg_harness'"
        ).fetchone()
    assert row == ("harness", "harness", "harness")


def test_run_migrations_strips_vault_secret_preview_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260621_0023")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        assert version == ("20260621_0023",)
        conn.executemany(
            """
            insert into vault_secrets (
                id, name, tags, kind, protection, source,
                ciphertext, nonce, wrap_meta, public_meta, policy,
                use_count, created_at, updated_at
            ) values (?, ?, null, 'static', 'standard', 'manual',
                'ct', 'nonce', 'wrap', ?, null, 0, 'now', 'now')
            """,
            [
                ("vlt_keep", "KEEP_DESC", json.dumps({"description": "kept", "preview": "…1234", "pubkey": "pk"})),
                ("vlt_empty", "ONLY_PREVIEW", json.dumps({"preview": "…9999"})),
                ("vlt_null", "NULL_META", None),
                ("vlt_blank", "BLANK_META", ""),
                ("vlt_bad", "BAD_META", "not-json"),
                ("vlt_other", "OTHER_META", json.dumps({"description": "other"})),
            ],
        )
        conn.commit()

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        rows = dict(conn.execute("select name, public_meta from vault_secrets order by name").fetchall())

    assert version == (HEAD_REVISION,)
    assert json.loads(rows["KEEP_DESC"]) == {"description": "kept", "pubkey": "pk"}
    assert rows["ONLY_PREVIEW"] is None
    assert rows["NULL_META"] is None
    assert rows["BLANK_META"] == ""
    assert rows["BAD_META"] == "not-json"
    assert json.loads(rows["OTHER_META"]) == {"description": "other"}
    assert "preview" not in json.dumps(rows)
    assert "1234" not in json.dumps(rows)
    assert "9999" not in json.dumps(rows)


def test_vault_snapshot_uses_final_grant_id_readiness_model(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260621_0023")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        columns = {row[1] for row in conn.execute('pragma table_info("vault_grants")')}

    assert version == ("20260621_0023",)
    assert {
        "id",
        "member_snapshot",
        "source_selector",
        "request_id",
        "session_id",
        "purpose",
        "one_shot",
        "expires_at",
        "agent_ready",
        "agent_ready_at",
    } <= columns
    assert "scope_type" not in columns
    assert "scope_ref" not in columns

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        columns = {row[1] for row in conn.execute('pragma table_info("vault_grants")')}

    assert version == (HEAD_REVISION,)
    assert {"request_id", "session_id", "purpose", "agent_ready", "agent_ready_at"} <= columns
    assert "scope_type" not in columns
    assert "scope_ref" not in columns


def test_vault_links_are_preserved_as_skill_tags_before_drop(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            insert into vault_secrets (
                id, name, tags, kind, protection, source,
                ciphertext, nonce, wrap_meta, public_meta, policy,
                use_count, created_at, updated_at
            ) values (?, ?, ?, 'static', 'standard', 'manual',
                'ct', 'nonce', 'wrap', null, null, 0, 'now', 'now')
            """,
            [
                ("vlt_a", "A_KEY", json.dumps(["existing"])),
                ("vlt_b", "B_KEY", None),
            ],
        )
        conn.execute(
            """
            create table vault_links (
                id text primary key,
                secret_name text not null,
                skill_name text not null,
                source text not null default 'agent',
                required integer not null default 0,
                created_at text not null,
                unique(secret_name, skill_name)
            )
            """
        )
        conn.execute("create index ix_vault_links_skill on vault_links(skill_name)")
        conn.executemany(
            """
            insert into vault_links (id, secret_name, skill_name, source, required, created_at)
            values (?, ?, ?, 'agent', 0, 'now')
            """,
            [
                ("lnk_1", "A_KEY", "deploy"),
                ("lnk_2", "A_KEY", "skill:release"),
                ("lnk_3", "B_KEY", "deploy"),
            ],
        )
        conn.commit()

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
        rows = dict(conn.execute("select name, tags from vault_secrets order by name").fetchall())

    assert "vault_links" not in tables
    assert json.loads(rows["A_KEY"]) == ["existing", "skill:deploy", "skill:release"]
    assert json.loads(rows["B_KEY"]) == ["skill:deploy"]


def test_run_migrations_expires_legacy_pending_access_cards(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260627_0025")

    now = "2026-07-03T00:00:00+00:00"
    legacy_delivery = json.dumps(
        {
            "card": {
                "card_type": "approval",
                "scope_options": [{"scope_type": "secret", "scope_ref": "A_KEY"}],
            }
        }
    )
    current_delivery = json.dumps(
        {
            "card": {
                "card_type": "approval",
                "grant_options": [
                    {
                        "grant_id": "vgr_ready",
                        "member_snapshot": ["B_KEY"],
                        "source_selector": {"env": ["B_KEY"]},
                    }
                ],
            }
        }
    )
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            insert into vault_requests (
                id, request_type, secret_name, requester, delivery, status,
                message_id, created_at, decided_at, expires_at
            ) values (?, ?, ?, null, ?, 'pending', null, ?, null, null)
            """,
            [
                ("req_legacy", "access", "A_KEY", legacy_delivery, now),
                ("req_current", "access", "B_KEY", current_delivery, now),
                ("req_sign", "sign", "SIGNING_KEY", legacy_delivery, now),
            ],
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = {
            row[0]: (row[1], row[2])
            for row in conn.execute("select id, status, decided_at from vault_requests order by id").fetchall()
        }

    assert rows["req_legacy"][0] == "expired"
    assert rows["req_legacy"][1] is not None
    assert rows["req_current"] == ("pending", None)
    assert rows["req_sign"] == ("pending", None)


def test_run_migrations_adds_case_folded_vault_secret_name_index(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260703_0026")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into vault_secrets (
                id, name, tags, kind, protection, source,
                ciphertext, nonce, wrap_meta, public_meta, policy,
                use_count, created_at, updated_at
            ) values ('vlt_a', 'openAiKey', null, 'static', 'standard', 'manual',
                'ct', 'nonce', 'wrap', null, null, 0, 'now', 'now')
            """
        )
        conn.commit()

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        indexes = {row[1] for row in conn.execute("select seq, name from pragma_index_list('vault_secrets')")}
        triggers = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'trigger' and tbl_name = 'vault_requests'"
            )
        }
        assert version == (HEAD_REVISION,)
        assert "uq_vault_secrets_name_folded" in indexes
        assert "lower(name)" in _index_sql(conn, "uq_vault_secrets_name_folded").lower()
        assert "trg_vault_requests_pending_provision_name_case_insert" in triggers
        assert "trg_vault_requests_pending_provision_name_case_update" in triggers
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into vault_secrets (
                    id, name, tags, kind, protection, source,
                    ciphertext, nonce, wrap_meta, public_meta, policy,
                    use_count, created_at, updated_at
                ) values ('vlt_b', 'OpenAIKey', null, 'static', 'standard', 'manual',
                    'ct', 'nonce', 'wrap', null, null, 0, 'now', 'now')
                """
            )
        conn.execute(
            """
            insert into vault_requests (
                id, request_type, secret_name, requester, delivery, status,
                message_id, created_at, decided_at, expires_at
            ) values ('vrq_a', 'provision', 'openAiKey', null, '{}', 'pending', null, 'now', null, null)
            """
        )
        conn.execute(
            """
            insert into vault_requests (
                id, request_type, secret_name, requester, delivery, status,
                message_id, created_at, decided_at, expires_at
            ) values ('vrq_exact_duplicate', 'provision', 'openAiKey', null, '{}', 'pending', null, 'now', null, null)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into vault_requests (
                    id, request_type, secret_name, requester, delivery, status,
                    message_id, created_at, decided_at, expires_at
                ) values ('vrq_b', 'provision', 'OpenAIKey', null, '{}', 'pending', null, 'now', null, null)
                """
            )


def test_scope_agent_backfill_migrates_explicit_agent_routes(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260526_0006")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model,
                reasoning_effort, system_prompt, enabled, source, source_ref,
                metadata_json, created_at, updated_at
            ) values (
                'agent_reviewer', 'Code Reviewer', 'code-reviewer', null, 'codex', null,
                null, null, 1, 'user', null, '{}', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, is_private, supports_threads,
                metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'scope_agent', 'slack', 'channel', 'C_AGENT', 0, 1,
                '{}', 'now', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend,
                agent_variant, model, reasoning_effort, require_mention,
                settings_version, settings_json, created_at, updated_at
            ) values (
                'scope_agent', 1, null, '/tmp/project', '', '', '', '', '',
                null, 1, ?, 'now', 'now'
            )
            """,
            (
                json.dumps(
                    {
                        "routing": {
                            "agent_name": "Code Reviewer",
                            "codex_agent": "reviewer-sub",
                            "model": "gpt-5.5",
                            "reasoning_effort": "xhigh",
                        }
                    }
                ),
            ),
        )
        conn.commit()

    run_migrations(db_path, revision="20260529_0007")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            select agent_name, agent_variant, model, reasoning_effort
              from scope_settings
             where scope_id = 'scope_agent'
            """
        ).fetchone()

    assert row == ("Code Reviewer", "reviewer-sub", "gpt-5.5", "xhigh")


def test_scope_agent_backfill_ignores_backend_only_routes(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260526_0006")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, is_private, supports_threads,
                metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'scope_backend_only', 'slack', 'channel', 'C_BACKEND_ONLY', 0, 1,
                '{}', 'now', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend,
                agent_variant, model, reasoning_effort, require_mention,
                settings_version, settings_json, created_at, updated_at
            ) values (
                'scope_backend_only', 1, null, '/tmp/project', '', 'opencode',
                'legacy-subagent', '', '', null, 1, ?, 'now', 'now'
            )
            """,
            (
                json.dumps(
                    {
                        "routing": {
                            "agent_backend": "opencode",
                            "model": "gpt-5.5",
                            "reasoning_effort": "high",
                        }
                    }
                ),
            ),
        )
        conn.commit()

    run_migrations(db_path, revision="20260529_0007")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            select agent_name, agent_variant, model, reasoning_effort
              from scope_settings
             where scope_id = 'scope_backend_only'
            """
        ).fetchone()

    assert row == ("", "legacy-subagent", "", "")


def test_run_migrations_deletes_historical_message_tool_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260608_0020")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, is_private, supports_threads,
                metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'scope_cleanup', 'avibe', 'project', 'proj_cleanup', 0, 0,
                '{}', 'now', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, content_text,
                content_json, metadata_json, created_at, updated_at
            ) values
                (
                    'msg_tool', 'scope_cleanup', null, 'avibe', 'agent', 'tool_call',
                    'ran tool', '{"text":"ran tool"}', '{}', 'now', 'now'
                ),
                (
                    'msg_result', 'scope_cleanup', null, 'avibe', 'agent', 'result',
                    'done', '{"text":"done"}', '{}', 'now', 'now'
                )
            """
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json, payload_json,
                message_id, created_at
            ) values
                (
                    'show_tool', 'ses_cleanup', 'annotation', 'agent', 'session',
                    '{}', '{}', 'msg_tool', 'now'
                ),
                (
                    'show_result', 'ses_cleanup', 'annotation', 'agent', 'session',
                    '{}', '{}', 'msg_result', 'now'
                )
            """
        )
        conn.execute(
            """
            insert into media_objects (
                token, scope_id, message_id, kind, source, local_path, created_at
            ) values
                (
                    'media_tool', 'scope_cleanup', 'msg_tool', 'file', 'agent',
                    '/tmp/tool.txt', 'now'
                ),
                (
                    'media_result', 'scope_cleanup', 'msg_result', 'file', 'agent',
                    '/tmp/result.txt', 'now'
                )
            """
        )
        conn.commit()

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        rows = conn.execute("select id, type from messages order by id").fetchall()
        show_refs = conn.execute("select id, message_id from show_session_events order by id").fetchall()
        media_refs = conn.execute("select token, message_id from media_objects order by token").fetchall()
    assert version == (HEAD_REVISION,)
    assert rows == [("msg_result", "result")]
    assert show_refs == [("show_result", "msg_result"), ("show_tool", None)]
    assert media_refs == [("media_result", "msg_result"), ("media_tool", None)]


def test_run_migrations_deletes_tool_calls_when_stamping_unversioned_head_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, is_private, supports_threads,
                metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'scope_stamp_cleanup', 'avibe', 'project', 'proj_stamp_cleanup', 0, 0,
                '{}', 'now', 'now', 'now'
            )
            """
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, content_text,
                content_json, metadata_json, created_at, updated_at
            ) values
                (
                    'msg_stamp_tool', 'scope_stamp_cleanup', null, 'avibe', 'agent', 'tool_call',
                    'ran tool', '{"text":"ran tool"}', '{}', 'now', 'now'
                ),
                (
                    'msg_stamp_result', 'scope_stamp_cleanup', null, 'avibe', 'agent', 'result',
                    'done', '{"text":"done"}', '{}', 'now', 'now'
                )
            """
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json, payload_json,
                message_id, created_at
            ) values
                (
                    'show_stamp_tool', 'ses_stamp_cleanup', 'annotation', 'agent', 'session',
                    '{}', '{}', 'msg_stamp_tool', 'now'
                ),
                (
                    'show_stamp_result', 'ses_stamp_cleanup', 'annotation', 'agent', 'session',
                    '{}', '{}', 'msg_stamp_result', 'now'
                )
            """
        )
        conn.execute(
            """
            insert into media_objects (
                token, scope_id, message_id, kind, source, local_path, created_at
            ) values
                (
                    'media_stamp_tool', 'scope_stamp_cleanup', 'msg_stamp_tool', 'file', 'agent',
                    '/tmp/stamp-tool.txt', 'now'
                ),
                (
                    'media_stamp_result', 'scope_stamp_cleanup', 'msg_stamp_result', 'file', 'agent',
                    '/tmp/stamp-result.txt', 'now'
                )
            """
        )
        conn.commit()
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        rows = conn.execute("select id, type from messages order by id").fetchall()
        show_refs = conn.execute("select id, message_id from show_session_events order by id").fetchall()
        media_refs = conn.execute("select token, message_id from media_objects order by token").fetchall()
    assert version == (HEAD_REVISION,)
    assert rows == [("msg_stamp_result", "result")]
    assert show_refs == [("show_stamp_result", "msg_stamp_result"), ("show_stamp_tool", None)]
    assert media_refs == [("media_stamp_result", "msg_stamp_result"), ("media_stamp_tool", None)]


def test_run_migrations_runs_legacy_default_cleanup_when_stamping_existing_head_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
            );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            """
        )
        conn.commit()
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        agents = dict(conn.execute("select name, backend from agents"))
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]

    assert version == (HEAD_REVISION,)
    assert "default" not in agents
    assert agents["opencode"] == "opencode"
    assert json.loads(default_pointer) == "opencode"


def test_run_migrations_stamps_pre_show_events_head_schema_at_0008_then_upgrades(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
            );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            """
        )
        conn.execute("drop table show_session_events")
        conn.execute("drop index if exists ix_show_session_events_session_created")
        conn.execute("drop index if exists ix_show_session_events_type_created")
        conn.execute("drop table web_push_subscriptions")
        conn.commit()
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None
        assert conn.execute("select name from sqlite_master where name = 'show_session_events'").fetchone() is None
        assert conn.execute("select name from sqlite_master where name = 'web_push_subscriptions'").fetchone() is None

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        show_events = conn.execute("select name from sqlite_master where name = 'show_session_events'").fetchone()
        web_push_columns = {row[1] for row in conn.execute("pragma table_info(web_push_subscriptions)")}
        background_tables = conn.execute("select count(*) from run_definitions").fetchone()
        agents = dict(conn.execute("select name, backend from agents"))
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]
    assert version == (HEAD_REVISION,)
    assert show_events == ("show_session_events",)
    assert "device_id" in web_push_columns
    assert background_tables == (0,)
    assert "default" not in agents
    assert agents["opencode"] == "opencode"
    assert json.loads(default_pointer) == "opencode"


def test_run_migrations_stamps_existing_initial_schema_with_empty_version_table(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.commit()

    run_migrations(db_path)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert version == (HEAD_REVISION,)


def test_run_migrations_ignores_deprecated_scope_backend_when_stamping_existing_head_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into scopes (
                id, platform, scope_type, native_id, parent_scope_id, display_name, native_type,
                is_private, supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'slack::channel::C1', 'slack', 'channel', 'C1', null, null, null, 0, 1, '{}', 'now', 'now', 'now'
            );
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, require_mention, settings_version, settings_json, created_at, updated_at
            ) values (
                'slack::channel::C1', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                '{"routing":{"agent_backend":"codex"}}', 'now', 'now'
            );
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        agent_name = conn.execute("select agent_name from scope_settings").fetchone()[0]
        codex_agent = conn.execute("select backend from agents where name = 'codex'").fetchone()

    assert version == (HEAD_REVISION,)
    assert agent_name is None
    assert codex_agent is None


def test_run_migrations_repairs_head_columns_before_stamping_head(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        columns = [
            row
            for row in conn.execute("pragma table_info(run_definitions)").fetchall()
            if row[1] != "deleted_at"
        ]
        column_defs = []
        for _cid, name, column_type, not_null, default_value, primary_key in columns:
            definition = f'"{name}" {column_type or "TEXT"}'
            if primary_key:
                definition += " PRIMARY KEY"
            if not_null:
                definition += " NOT NULL"
            if default_value is not None:
                definition += f" DEFAULT {default_value}"
            column_defs.append(definition)
        conn.execute('alter table "run_definitions" rename to "run_definitions_old"')
        conn.execute(f'create table "run_definitions" ({", ".join(column_defs)})')
        conn.execute('drop table "run_definitions_old"')
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.commit()

    assert background_tables_ready(db_path) is False

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        background_columns = {row[1] for row in conn.execute("pragma table_info(run_definitions)")}
    assert version == (HEAD_REVISION,)
    assert "deleted_at" in background_columns
    assert background_tables_ready(db_path) is True


def test_background_tables_ready_requires_messages_type(tmp_path: Path) -> None:
    """A DB at the prior (20260530_0009) head — full tables but no messages.type —
    must report NOT ready so SQLiteBackgroundTaskStore triggers the migration;
    otherwise messages_service.append would write a column that doesn't exist."""
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute("drop index if exists ix_messages_session_type")
        conn.execute("drop index if exists ix_messages_session_type_created_id")
        conn.execute("drop index if exists ix_messages_unread_session")
        conn.execute("drop index if exists ix_messages_inbox_activity")
        conn.execute("drop index if exists ix_messages_inbox_agent_reply")
        conn.execute("drop index if exists ix_messages_inbox_user_send")
        conn.execute('alter table "messages" drop column "type"')
        conn.execute("create table if not exists alembic_version (version_num varchar(32) not null)")
        conn.execute("delete from alembic_version")
        conn.execute("insert into alembic_version values ('20260530_0009')")
        conn.commit()

    assert background_tables_ready(db_path) is False

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(messages)")}
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert "type" in columns
    assert version == (HEAD_REVISION,)
    assert background_tables_ready(db_path) is True


def test_run_migrations_repairs_head_stamped_background_schema_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute('alter table "run_definitions" rename column "definition_type" to "task_type"')
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.execute("insert into alembic_version values ('20260523_0004')")
        conn.commit()

    assert background_tables_ready(db_path) is False

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        columns = {row[1] for row in conn.execute("pragma table_info(run_definitions)")}
    assert version == (HEAD_REVISION,)
    assert "definition_type" in columns
    assert "task_type" not in columns
    assert background_tables_ready(db_path) is True


def test_run_migrations_backfills_existing_session_policy_only_for_targeted_definitions(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.execute("update run_definitions set session_policy = null")
        conn.execute(
            """
            insert into run_definitions (
                id, definition_type, name, session_id, legacy_session_key, message, enabled, created_at, updated_at,
                metadata_json
            )
            values
                ('with-session-id', 'watch', 'with session id', 'ses123', '', 'watch', 1, '2026-05-22T00:00:00+00:00', '2026-05-22T00:00:00+00:00', '{}'),
                ('with-session-key', 'watch', 'with session key', '', 'slack::channel::C123', 'watch', 1, '2026-05-22T00:00:00+00:00', '2026-05-22T00:00:00+00:00', '{}'),
                ('without-target', 'watch', 'without target', '', '', 'watch', 1, '2026-05-22T00:00:00+00:00', '2026-05-22T00:00:00+00:00', '{}')
            """
        )
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.execute("insert into alembic_version values ('20260515_0002')")
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("select id, session_policy from run_definitions where id like 'with%' or id = 'without-target'"))

    assert rows["with-session-id"] == "existing"
    assert rows["with-session-key"] == "existing"
    assert rows["without-target"] is None


def test_run_migrations_removes_legacy_builtin_default_agent(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values
                (
                    'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                    null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
                ),
                (
                    'agent-opencode', 'opencode', 'opencode', 'Default Agent for the opencode backend.', 'opencode',
                    null, null, null, 1, 'builtin', null,
                    '{"builtin":true,"builtin_default":true,"lock_delete":true,"backend":"opencode","backend_enabled":true}',
                    'now', 'now'
                );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            insert into scopes (
                id, platform, scope_type, native_id, parent_scope_id, display_name, native_type,
                is_private, supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values
                (
                    'slack::channel::C1', 'slack', 'channel', 'C1',
                    null, null, null, 0, 1, '{}', 'now', 'now', 'now'
                ),
                (
                    'discord::guild::G1', 'discord', 'guild', 'G1',
                    null, null, null, 0, 0, '{}', 'now', 'now', 'now'
                );
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, require_mention, settings_version, settings_json, created_at, updated_at
            ) values
                (
                    'slack::channel::C1', 1, null, '/repo', 'default', 'opencode', 'default',
                    null, null, null, 1,
                    '{"routing":{"agent_name":"default","agent":"default","agent_backend":"opencode"}}',
                    'now', 'now'
                ),
                (
                    'discord::guild::G1', 1, null, null, null, 'opencode', null,
                    null, null, null, 1,
                    '{"routing":{"agent_name":"default","agent":"default","agent_backend":"opencode"}}',
                    'now', 'now'
                );
            insert into agent_sessions (
                id, scope_id, agent_id, agent_name, agent_backend, agent_variant, model, reasoning_effort,
                session_anchor, workdir, native_session_id, title, status, metadata_json, created_at, updated_at
            ) values (
                'session-1', 'slack::channel::C1', 'agent-default', 'default', 'opencode', 'default',
                null, null, 'thread-1', '/repo', 'native-1', null, 'active', '{}', 'now', 'now'
            );
            insert into run_definitions (
                id, definition_type, name, agent_name, session_policy, message, enabled, created_at, updated_at,
                metadata_json
            ) values (
                'definition-1', 'task', 'task', 'default', 'new', 'hello', 1, 'now', 'now', '{}'
            );
            insert into agent_runs (
                id, run_type, status, agent_name, agent_id, agent_backend, message, created_at, updated_at,
                metadata_json, cancel_requested
            ) values (
                'run-1', 'task', 'queued', 'default', 'agent-default', 'opencode', 'hello', 'now', 'now', '{}', 0
            );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agents = dict(conn.execute("select name, id from agents"))
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]
        scope_agent, scope_variant, settings_json = conn.execute(
            "select agent_name, agent_variant, settings_json from scope_settings where scope_id = 'slack::channel::C1'"
        ).fetchone()
        json_only_scope_agent, json_only_scope_variant, json_only_settings_json = conn.execute(
            "select agent_name, agent_variant, settings_json from scope_settings where scope_id = 'discord::guild::G1'"
        ).fetchone()
        session_agent = conn.execute(
            "select agent_id, agent_name, agent_variant from agent_sessions where id = 'session-1'"
        ).fetchone()
        definition_agent = conn.execute(
            "select agent_name from run_definitions where id = 'definition-1'"
        ).fetchone()[0]
        run_agent = conn.execute(
            "select agent_id, agent_name from agent_runs where id = 'run-1'"
        ).fetchone()
        version = conn.execute("select version_num from alembic_version").fetchone()

    payload = json.loads(settings_json)
    json_only_payload = json.loads(json_only_settings_json)
    assert version == (HEAD_REVISION,)
    assert "default" not in agents
    assert agents["opencode"] == "agent-opencode"
    assert json.loads(default_pointer) == "opencode"
    assert scope_agent == "opencode"
    assert scope_variant == "opencode"
    assert payload["routing"]["agent_name"] == "opencode"
    assert payload["routing"]["agent"] == "opencode"
    assert json_only_scope_agent == "opencode"
    assert json_only_scope_variant is None
    assert json_only_payload["routing"]["agent_name"] == "opencode"
    assert json_only_payload["routing"]["agent"] == "opencode"
    assert session_agent == ("agent-opencode", "opencode", "opencode")
    assert definition_agent == "opencode"
    assert run_agent == ("agent-opencode", "opencode")


def test_run_migrations_creates_backend_default_before_removing_legacy_default(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
            );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agents = {
            row[0]: {
                "id": row[1],
                "backend": row[2],
                "enabled": row[3],
                "source": row[4],
                "metadata": json.loads(row[5]),
            }
            for row in conn.execute("select name, id, backend, enabled, source, metadata_json from agents")
        }
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert set(agents) == {"opencode"}
    assert agents["opencode"]["id"] != "agent-default"
    assert agents["opencode"]["backend"] == "opencode"
    assert agents["opencode"]["enabled"] == 1
    assert agents["opencode"]["source"] == "builtin"
    assert agents["opencode"]["metadata"] == {
        "builtin": True,
        "builtin_default": True,
        "lock_delete": True,
        "backend": "opencode",
        "backend_enabled": True,
    }
    assert json.loads(default_pointer) == "opencode"


def test_run_migrations_removes_unreferenced_disabled_legacy_default_with_existing_backend_default(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values
                (
                    'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                    null, null, null, 0, 'builtin', null, '{"builtin":true}', 'now', 'now'
                ),
                (
                    'agent-opencode', 'opencode', 'opencode', 'Default Agent for the opencode backend.', 'opencode',
                    null, null, null, 1, 'builtin', null,
                    '{"builtin":true,"builtin_default":true,"lock_delete":true,"backend":"opencode","backend_enabled":true}',
                    'now', 'now'
                );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agents = dict(conn.execute("select name, enabled from agents order by name"))
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert agents == {"opencode": 1}


def test_run_migrations_skips_disabled_legacy_default_with_existing_backend_target_references(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values
                (
                    'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                    null, null, null, 0, 'builtin', null, '{"builtin":true}', 'now', 'now'
                ),
                (
                    'agent-opencode', 'opencode', 'opencode', 'Default Agent for the opencode backend.', 'opencode',
                    null, null, null, 1, 'builtin', null,
                    '{"builtin":true,"builtin_default":true,"lock_delete":true,"backend":"opencode","backend_enabled":true}',
                    'now', 'now'
                );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agents = dict(conn.execute("select name, enabled from agents order by name"))
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert agents == {"default": 0, "opencode": 1}
    assert json.loads(default_pointer) == "default"


def test_run_migrations_preserves_disabled_legacy_default_when_creating_backend_default(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                null, null, null, 0, 'builtin', null, '{"builtin":true}', 'now', 'now'
            );
            insert into state_meta (key, value_json, updated_at)
            values ('default_agent_name', '"default"', 'now');
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agent = conn.execute(
            "select name, backend, enabled, source, metadata_json from agents"
        ).fetchone()
        default_pointer = conn.execute(
            "select value_json from state_meta where key = 'default_agent_name'"
        ).fetchone()[0]
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert agent[0:4] == ("opencode", "opencode", 0, "builtin")
    assert json.loads(agent[4]) == {
        "builtin": True,
        "builtin_default": True,
        "lock_delete": True,
        "backend": "opencode",
        "backend_enabled": True,
    }
    assert json.loads(default_pointer) == "opencode"


def test_run_migrations_skips_user_owned_default_agent(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-default', 'default', 'default', 'User default agent.', 'opencode',
                null, null, 'custom prompt', 1, 'user', null, '{}', 'now', 'now'
            );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        default_agent = conn.execute(
            "select source, system_prompt from agents where normalized_name = 'default'"
        ).fetchone()
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert default_agent == ("user", "custom prompt")


def test_run_migrations_skips_legacy_default_when_backend_target_is_user_agent(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values
                (
                    'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                    null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
                ),
                (
                    'agent-opencode-user', 'opencode', 'opencode', 'User opencode agent.', 'opencode',
                    null, null, 'custom prompt', 1, 'user', null, '{}', 'now', 'now'
                );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("select name, source from agents order by name"))
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert rows == {"default": "builtin", "opencode": "user"}


def test_run_migrations_skips_legacy_default_when_backend_target_is_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values
                (
                    'agent-default', 'default', 'default', 'Default Vibe Remote agent.', 'opencode',
                    null, null, null, 1, 'builtin', null, '{"builtin":true}', 'now', 'now'
                ),
                (
                    'agent-opencode-disabled', 'opencode', 'opencode', 'Default Agent for the opencode backend.', 'opencode',
                    null, null, null, 0, 'builtin', null,
                    '{"builtin":true,"builtin_default":true,"lock_delete":true,"backend":"opencode","backend_enabled":true}',
                    'now', 'now'
                );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260529_0007');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("select name, enabled from agents order by name"))
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert rows == {"default": 1, "opencode": 0}


def test_run_migrations_ignores_deprecated_scope_backend_route(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-claude', 'claude', 'claude', 'Default Agent for the claude backend.', 'claude', null, null,
                null, 1, 'builtin', null, '{}', 'now', 'now'
            );
            insert into scopes (
                id, platform, scope_type, native_id, parent_scope_id, display_name, native_type,
                is_private, supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values
                ('slack::channel::C1', 'slack', 'channel', 'C1', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::user::U1', 'slack', 'user', 'U1', null, null, null, 1, 0, '{}', 'now', 'now', 'now'),
                ('slack::channel::C2', 'slack', 'channel', 'C2', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::channel::C3', 'slack', 'channel', 'C3', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::channel::C4', 'slack', 'channel', 'C4', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::channel::C5', 'slack', 'channel', 'C5', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::channel::C6', 'slack', 'channel', 'C6', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('discord::guild::G1', 'discord', 'guild', 'G1', null, null, null, 0, 0, '{}', 'now', 'now', 'now');
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, require_mention, settings_version, settings_json, created_at, updated_at
            ) values
                ('slack::channel::C1', 1, null, '/repo', null, 'codex', null, 'gpt-5.5', 'high', 0, 1,
                 '{"show_message_types":["assistant"],"routing":{"agent_backend":"codex","codex_model":"gpt-5.5"}}', 'now', 'now'),
                ('slack::user::U1', 1, 'admin', '/repo', null, 'claude', null, null, null, null, 1,
                 '{"routing":{"agent_backend":"claude"}}', 'now', 'now'),
                ('slack::channel::C2', 1, null, '/repo', 'reviewer', 'codex', null, null, null, null, 1,
                 '{"routing":{"agent_name":"reviewer","agent_backend":"codex"}}', 'now', 'now'),
                ('slack::channel::C3', 1, null, '/repo', null, null, null, null, null, null, 1,
                 '{"routing":{}}', 'now', 'now'),
                ('slack::channel::C4', 1, null, '/repo', null, 'claude', null, null, null, null, 1,
                 'not-json', 'now', 'now'),
                ('slack::channel::C5', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                 '{"routing":{"agent_name":"reviewer","agent_backend":"codex"}}', 'now', 'now'),
                ('slack::channel::C6', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                 '{"routing":{"agent":"legacy-reviewer","agent_backend":"codex"}}', 'now', 'now'),
                ('discord::guild::G1', 1, null, null, null, 'opencode', null, null, null, null, 1,
                 '{"routing":{"agent_backend":"opencode"}}', 'now', 'now');
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260526_0006');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("select scope_id, agent_name from scope_settings"))
        codex_agent = conn.execute("select backend from agents where name = 'codex'").fetchone()
        claude_agent_count = conn.execute("select count(*) from agents where name = 'claude'").fetchone()[0]
        payload = json.loads(
            conn.execute(
                "select settings_json from scope_settings where scope_id = 'slack::channel::C1'"
            ).fetchone()[0]
        )
        malformed_json = conn.execute(
            "select settings_json from scope_settings where scope_id = 'slack::channel::C4'"
        ).fetchone()[0]
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert rows["slack::channel::C1"] is None
    assert rows["slack::user::U1"] is None
    assert rows["slack::channel::C2"] == "reviewer"
    assert rows["slack::channel::C3"] is None
    assert rows["slack::channel::C4"] is None
    assert rows["slack::channel::C5"] is None
    assert rows["slack::channel::C6"] is None
    assert rows["discord::guild::G1"] is None
    assert codex_agent is None
    assert claude_agent_count == 1
    assert "agent_name" not in payload["routing"]
    assert payload["routing"]["codex_model"] == "gpt-5.5"
    assert malformed_json == "not-json"


def test_run_migrations_leaves_deprecated_scope_backend_unresolved_on_agent_name_conflict(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-codex-conflict', 'codex', 'codex', 'User codex alias.', 'opencode', null, null,
                null, 1, 'user', null, '{}', 'now', 'now'
            );
            insert into scopes (
                id, platform, scope_type, native_id, parent_scope_id, display_name, native_type,
                is_private, supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values
                ('slack::channel::C1', 'slack', 'channel', 'C1', null, null, null, 0, 1, '{}', 'now', 'now', 'now'),
                ('slack::channel::C2', 'slack', 'channel', 'C2', null, null, null, 0, 1, '{}', 'now', 'now', 'now');
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, require_mention, settings_version, settings_json, created_at, updated_at
            ) values
                ('slack::channel::C1', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                 '{"routing":{"agent_backend":"codex"}}', 'now', 'now'),
                ('slack::channel::C2', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                 '{"routing":{"agent_name":"reviewer","agent_backend":"codex"}}', 'now', 'now');
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260526_0006');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("select scope_id, agent_name from scope_settings"))
        codex_rows = conn.execute("select count(*) from agents where normalized_name = 'codex'").fetchone()[0]
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert version == (HEAD_REVISION,)
    assert rows["slack::channel::C1"] is None
    assert rows["slack::channel::C2"] is None
    assert codex_rows == 1


def test_run_migrations_skips_disabled_backend_agent_name_match(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-codex-disabled', 'codex', 'codex', 'Disabled codex agent.', 'codex', null, null,
                null, 0, 'user', null, '{}', 'now', 'now'
            );
            insert into scopes (
                id, platform, scope_type, native_id, parent_scope_id, display_name, native_type,
                is_private, supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'slack::channel::C1', 'slack', 'channel', 'C1', null, null, null, 0, 1, '{}', 'now', 'now', 'now'
            );
            insert into scope_settings (
                scope_id, enabled, role, workdir, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, require_mention, settings_version, settings_json, created_at, updated_at
            ) values (
                'slack::channel::C1', 1, null, '/repo', null, 'codex', null, null, null, null, 1,
                '{"routing":{"agent_backend":"codex"}}', 'now', 'now'
            );
            create table alembic_version (version_num varchar(32) not null);
            insert into alembic_version values ('20260526_0006');
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        agent_name = conn.execute("select agent_name from scope_settings").fetchone()[0]
        codex_rows = conn.execute("select count(*) from agents where normalized_name = 'codex'").fetchone()[0]

    assert agent_name is None
    assert codex_rows == 1


def test_run_migrations_does_not_stamp_partial_schema_missing_scopes(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            create table state_meta (
                key varchar primary key,
                value_json text not null,
                updated_at varchar not null
            );
            create table scope_settings (
                scope_id varchar primary key,
                enabled integer not null,
                role varchar,
                workdir text,
                agent_backend varchar,
                agent_variant varchar,
                model varchar,
                reasoning_effort varchar,
                require_mention integer,
                settings_version integer not null,
                settings_json text not null,
                created_at varchar not null,
                updated_at varchar not null
            );
            create table auth_codes (
                code varchar primary key,
                type varchar not null,
                is_active integer not null,
                expires_at varchar,
                used_by_json text not null,
                created_at varchar not null,
                updated_at varchar not null
            );
            create table agent_sessions (
                id varchar primary key,
                scope_id varchar,
                agent_backend varchar not null,
                agent_variant varchar not null,
                model varchar,
                reasoning_effort varchar,
                session_anchor varchar not null,
                workdir text,
                native_session_id text not null,
                title text,
                status varchar not null,
                metadata_json text not null,
                created_at varchar not null,
                updated_at varchar not null,
                last_active_at varchar
            );
            create table runtime_records (
                id varchar primary key,
                record_type varchar not null,
                record_key varchar not null,
                scope_id varchar,
                session_anchor varchar,
                workdir text,
                payload_json text not null,
                expires_at varchar,
                created_at varchar not null,
                updated_at varchar not null
            );
            create table alembic_version (version_num varchar(32) not null);
            """
        )
        conn.commit()

    with pytest.raises(Exception, match="scopes"):
        run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        assert version is None
        assert conn.execute("select name from sqlite_master where name = 'scopes'").fetchone() is None


def _insert_scope(conn: sqlite3.Connection, scope_id: str) -> None:
    conn.execute(
        """
        insert into scopes (
            id, platform, scope_type, native_id, is_private, supports_threads,
            metadata_json, first_seen_at, last_seen_at, updated_at
        ) values (?, 'slack', 'channel', ?, 0, 1, '{}', 'now', 'now', 'now')
        """,
        (scope_id, scope_id),
    )


def _insert_agent_session(
    conn: sqlite3.Connection,
    *,
    row_id: str,
    scope_id: str,
    anchor: str,
    workdir: str | None,
    backend: str,
    native: str,
    last_active: str,
) -> None:
    conn.execute(
        """
        insert into agent_sessions (
            id, scope_id, agent_backend, agent_variant, session_anchor, workdir,
            native_session_id, status, metadata_json, created_at, updated_at, last_active_at
        ) values (?, ?, ?, ?, ?, ?, ?, 'active', '{}', 'now', 'now', ?)
        """,
        (row_id, scope_id, backend, backend, anchor, workdir, native, last_active),
    )


def test_run_migrations_session_anchor_unique_strips_dedups_and_reattaches(tmp_path: Path) -> None:
    # Build the pre-0011 schema, seed the exact legacy states 0011 must handle,
    # then upgrade to head and assert the three guarantees: OpenCode cwd anchors
    # collapse to the bare base, claude/codex subagent
    # anchors are PRESERVED, duplicate (scope, anchor) rows dedup to the most
    # recent, and the loser's transcript is reattached to the survivor first.
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260531_0010")

    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "sc1")
        # OpenCode cwd composite -> stripped to bare base.
        _insert_agent_session(
            conn, row_id="ses_oc0000001", scope_id="sc1", anchor="oc-base:/repo/x",
            workdir="/repo/x", backend="opencode", native="oc-native", last_active="2026-06-01T08:00:00",
        )
        # claude SUBAGENT anchor (non-path suffix) -> preserved.
        _insert_agent_session(
            conn, row_id="ses_sub0000001", scope_id="sc1", anchor="cl-base:reviewer",
            workdir="reviewer", backend="claude", native="sub-native", last_active="2026-06-01T08:00:00",
        )
        # Windows OpenCode cwd composite (drive-letter colon) -> bare base,
        # without deriving workdir from the anchor suffix.
        _insert_agent_session(
            conn, row_id="ses_oswin00001", scope_id="sc1", anchor="win-base:C:\\repo\\x",
            workdir=None, backend="opencode", native="win-native2", last_active="2026-06-01T08:00:00",
        )
        # Duplicate group: a bare row + a cwd composite that strips onto it. The
        # later last_active row survives; the loser carries a transcript.
        _insert_agent_session(
            conn, row_id="ses_win0000001", scope_id="sc1", anchor="dup-base",
            workdir=None, backend="claude", native="win-native", last_active="2026-06-01T10:00:00",
        )
        _insert_agent_session(
            conn, row_id="ses_lose000001", scope_id="sc1", anchor="dup-base:/cwd",
            workdir="/cwd", backend="opencode", native="lose-native", last_active="2026-06-01T09:00:00",
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type,
                content_json, metadata_json, created_at, updated_at
            ) values ('msg1', 'sc1', 'ses_lose000001', 'slack', 'agent', 'assistant',
                      '{}', '{}', 'now', 'now')
            """,
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute("select id, session_anchor, workdir from agent_sessions")
        }
        # OpenCode cwd stripped to bare base; existing workdir retained, but not
        # derived from the anchor suffix.
        assert rows["ses_oc0000001"] == ("oc-base", "/repo/x")
        # Subagent anchor preserved (Codex P2: do not collapse base:<subagent>).
        assert rows["ses_sub0000001"] == ("cl-base:reviewer", "reviewer")
        # Windows drive-letter cwd stripped to bare base; workdir remains empty.
        assert rows["ses_oswin00001"] == ("win-base", None)
        # Dedup kept the most-recently-active row; the loser is gone.
        assert "ses_win0000001" in rows
        assert "ses_lose000001" not in rows
        assert len(rows) == 4
        # Transcript reattached to the survivor before the loser was deleted
        # (Codex P2: ondelete=SET NULL would otherwise orphan it).
        msg_session = conn.execute("select session_id from messages where id = 'msg1'").fetchone()
        assert msg_session == ("ses_win0000001",)
        # The invariant is enforced going forward.
        index = conn.execute(
            "select name from sqlite_master where type = 'index' and name = 'uq_agent_sessions_scope_anchor'"
        ).fetchone()
        assert index == ("uq_agent_sessions_scope_anchor",)


def test_ensure_sqlite_state_imports_json_once(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    _write_current_settings(state_dir / "settings.json")
    _write_current_sessions(state_dir / "sessions.json")
    _write_discovered_chats(state_dir / "discovered_chats.json")

    first = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")
    second = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    assert first.imported is True
    assert first.backup_path is not None
    assert (first.backup_path / "settings.json").exists()
    assert first.counts["scopes"] == 5
    assert first.counts["scope_settings"] == 4
    assert first.counts["auth_codes"] == 1
    assert first.counts["agent_sessions"] == 1
    assert first.counts["runtime_records"] == 4
    assert first.counts["discovered_scopes"] == 1
    with sqlite3.connect(db_path) as conn:
        last_activity = conn.execute(
            "select value_json from state_meta where key = 'sessions_last_activity'",
        ).fetchone()
        channel_settings = conn.execute(
            """
            select s.native_id, ss.workdir, ss.agent_name, ss.agent_backend, ss.model, ss.reasoning_effort
            from scopes s
            join scope_settings ss on ss.scope_id = s.id
            where s.platform = 'slack' and s.scope_type = 'channel' and s.native_id = 'C123'
            """,
        ).fetchone()
        user_settings = conn.execute(
            """
            select ss.role, ss.agent_name, ss.agent_backend
            from scopes s
            join scope_settings ss on ss.scope_id = s.id
            where s.platform = 'slack' and s.scope_type = 'user' and s.native_id = 'U123'
            """,
        ).fetchone()
        agent_session = conn.execute(
            """
            select id, scope_id, session_anchor, workdir, native_session_id, agent_name, agent_variant
            from agent_sessions
            """,
        ).fetchone()
        duplicate_insert_ok = True
        try:
            conn.execute(
                """
                insert into agent_sessions (
                    id, scope_id, agent_backend, agent_variant, model, reasoning_effort,
                    session_anchor, workdir, native_session_id, title, status,
                    metadata_json, created_at, updated_at, last_active_at
                ) values (
                    'sesabc234def', ?, 'codex', 'codex', 'gpt-5.4', 'high',
                    ?, ?, 'native-2', null, 'active', '{}', 'now', 'now', 'now'
                )
                """,
                (agent_session[1], agent_session[2], agent_session[3]),
            )
        except sqlite3.IntegrityError:
            duplicate_insert_ok = False
    assert last_activity == ('"2026-05-01T00:00:00+00:00"',)
    assert channel_settings == ("C123", "/repo", None, None, None, None)
    assert user_settings == ("admin", None, None)
    assert re.fullmatch(r"ses[23456789abcdefghjkmnpqrstuvwxyz]{10}", agent_session[0])
    assert agent_session[1] == "slack::channel::C123"
    # Legacy composite anchors are normalised to the bare anchor on import, but
    # workdir is snapshotted from scope settings rather than inferred from the
    # anchor suffix.
    assert agent_session[2] == "slack_1774074591.762089"
    assert agent_session[3] == "/repo"
    assert agent_session[4] == "codex-session-1"
    assert agent_session[5] is None
    assert agent_session[6] == "codex"
    # The (scope_id, session_anchor) unique index now rejects a second row for the
    # same thread — a thread is ONE session.
    assert duplicate_insert_ok is False

    assert second.imported is False
    assert second.backup_path is None
    assert second.counts == {
        key: value
        for key, value in first.counts.items()
        if key
        not in {
            "discovered_scopes",
            "background_scheduled_tasks",
            "background_watches",
            "background_runs_imported",
        }
    }


def test_ensure_sqlite_state_collapses_multi_backend_anchor_on_import(tmp_path: Path) -> None:
    # ensure_sqlite_state runs the migration (installing the (scope, anchor) unique
    # index) BEFORE importing sessions.json. Legacy JSON can list several backends
    # under ONE thread (pre-pin), so the import must collapse them onto a single
    # bare-anchor row instead of crashing on the unique index or leaving a composite
    # anchor the bare-anchor read path can't find. (Codex P2 #263.)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "session_mappings": {
                    "slack::C123": {
                        "claude": {"slack_T1": "claude-native"},
                        "codex": {"slack_T1": "codex-native"},
                        "opencode": {"slack_T1:/repo": "opencode-native"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("select session_anchor, workdir from agent_sessions").fetchall()
        # All three backends collapsed to ONE bare-anchor row — no IntegrityError,
        # no leftover ``slack_T1:/repo`` composite, and no anchor-derived workdir.
        assert len(rows) == 1
        assert rows[0][0] == "slack_T1"
        assert rows[0][1] is None
        index = conn.execute(
            "select name from sqlite_master where type = 'index' and name = 'uq_agent_sessions_scope_anchor'"
        ).fetchone()
        assert index == ("uq_agent_sessions_scope_anchor",)


def test_ensure_sqlite_state_import_skips_agent_name_conflict(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    _write_current_settings(state_dir / "settings.json")
    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agents (
                id, name, normalized_name, description, backend, model, reasoning_effort,
                system_prompt, enabled, source, source_ref, metadata_json, created_at, updated_at
            ) values (
                'agent-codex-conflict', 'codex', 'codex', 'User codex alias.', 'opencode', null, null,
                null, 1, 'user', null, '{}', 'now', 'now'
            )
            """
        )
        conn.commit()

    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        channel_agent_name = conn.execute(
            """
            select ss.agent_name
            from scopes s
            join scope_settings ss on ss.scope_id = s.id
            where s.scope_type = 'channel' and s.native_id = 'C123'
            """
        ).fetchone()[0]
        user_agent_name = conn.execute(
            """
            select ss.agent_name
            from scopes s
            join scope_settings ss on ss.scope_id = s.id
            where s.scope_type = 'user' and s.native_id = 'U123'
            """
        ).fetchone()[0]
        codex_rows = conn.execute("select count(*) from agents where normalized_name = 'codex'").fetchone()[0]

    assert channel_agent_name is None
    assert user_agent_name is None
    assert codex_rows == 1


def test_ensure_sqlite_state_preserves_backend_aliases_without_deprecated_backend(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path)

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::C123": ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(
                            claude_model="claude-opus-4-8",
                            claude_reasoning_effort="max",
                        ),
                    ),
                }
            )
        )
    finally:
        service.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into state_meta (key, value_json, updated_at) values (?, ?, ?)",
            (JSON_IMPORT_MARKER, '"2026-05-01T00:00:00+00:00"', "2026-05-01T00:00:00+00:00"),
        )
        conn.commit()

    first = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")
    second = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select agent_backend, model, reasoning_effort, settings_json from scope_settings where scope_id = ?",
            ("slack::channel::C123",),
        ).fetchone()

    routing = json.loads(row[3])["routing"]
    assert row[:3] == (None, None, None)
    assert routing["model"] is None
    assert routing["reasoning_effort"] is None
    assert routing["claude_model"] == "claude-opus-4-8"
    assert routing["claude_reasoning_effort"] == "max"
    assert "routing_scope_settings_migrated" not in first.counts
    assert "routing_scope_settings_migrated" not in second.counts


def test_ensure_sqlite_state_keeps_canonical_scope_routing_with_stale_alias(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path)

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::C123": ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(
                            model="claude-sonnet-4-6",
                            reasoning_effort="high",
                            claude_model="claude-opus-4-8",
                            claude_reasoning_effort="max",
                        ),
                    ),
                }
            )
        )
    finally:
        service.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into state_meta (key, value_json, updated_at) values (?, ?, ?)",
            (JSON_IMPORT_MARKER, '"2026-05-01T00:00:00+00:00"', "2026-05-01T00:00:00+00:00"),
        )
        conn.commit()

    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select model, reasoning_effort, settings_json from scope_settings where scope_id = ?",
            ("slack::channel::C123",),
        ).fetchone()

    routing = json.loads(row[2])["routing"]
    assert row[:2] == ("claude-sonnet-4-6", "high")
    assert routing["model"] == "claude-sonnet-4-6"
    assert routing["reasoning_effort"] == "high"
    assert routing["claude_model"] == "claude-opus-4-8"
    assert routing["claude_reasoning_effort"] == "max"


def test_ensure_sqlite_state_preserves_legacy_routing_without_backend(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    run_migrations(db_path)

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={
                    "slack::C123": ChannelSettings(
                        enabled=True,
                        routing=RoutingSettings(
                            claude_model="claude-opus-4-8",
                            claude_reasoning_effort="max",
                        ),
                    ),
                }
            )
        )
    finally:
        service.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into state_meta (key, value_json, updated_at) values (?, ?, ?)",
            (JSON_IMPORT_MARKER, '"2026-05-01T00:00:00+00:00"', "2026-05-01T00:00:00+00:00"),
        )
        conn.commit()

    result = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select agent_backend, model, reasoning_effort, settings_json from scope_settings where scope_id = ?",
            ("slack::channel::C123",),
        ).fetchone()

    routing = json.loads(row[3])["routing"]
    assert row[:3] == (None, None, None)
    assert routing["model"] is None
    assert routing["reasoning_effort"] is None
    assert routing["claude_model"] == "claude-opus-4-8"
    assert routing["claude_reasoning_effort"] == "max"
    assert "routing_scope_settings_migrated" not in result.counts

    store = SettingsStore(state_dir / "settings.json")
    try:
        channel = store.find_channel("C123", platform="slack")
        assert channel is not None
        assert channel.routing.model is None
        assert channel.routing.reasoning_effort is None
        assert channel.routing.claude_model == "claude-opus-4-8"
        assert channel.routing.claude_reasoning_effort == "max"
        store.update_channel("C999", ChannelSettings(enabled=True), platform="slack")
    finally:
        store.close()

    with sqlite3.connect(db_path) as conn:
        roundtrip_row = conn.execute(
            "select agent_backend, model, reasoning_effort, settings_json from scope_settings where scope_id = ?",
            ("slack::channel::C123",),
        ).fetchone()

    roundtrip_routing = json.loads(roundtrip_row[3])["routing"]
    assert roundtrip_row[:3] == (None, None, None)
    assert roundtrip_routing["model"] is None
    assert roundtrip_routing["reasoning_effort"] is None
    assert roundtrip_routing["claude_model"] == "claude-opus-4-8"
    assert roundtrip_routing["claude_reasoning_effort"] == "max"


def test_ensure_sqlite_state_imports_background_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "scheduled_tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "Digest",
                        "session_id": "sesk8m4q2p7x",
                        "session_key": "slack::channel::C123",
                        "prompt": "hello",
                        "schedule_type": "cron",
                        "cron": "0 * * * *",
                        "timezone": "UTC",
                        "enabled": True,
                        "created_at": "2026-05-15T00:00:00+00:00",
                        "updated_at": "2026-05-15T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "watches.json").write_text(
        json.dumps(
            {
                "watches": [
                    {
                        "id": "watch-1",
                        "name": "Watch CI",
                        "session_id": "sesk8m4q2p7x",
                        "session_key": "slack::channel::C123",
                        "command": ["python3", "wait.py"],
                        "mode": "forever",
                        "timeout_seconds": 600,
                        "lifetime_timeout_seconds": 3600,
                        "retry_exit_codes": [75],
                        "retry_delay_seconds": 30,
                        "enabled": True,
                        "created_at": "2026-05-15T00:00:00+00:00",
                        "updated_at": "2026-05-15T00:00:00+00:00",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    pending = state_dir / "task_requests" / "pending"
    pending.mkdir(parents=True)
    (pending / "hook-1.json").write_text(
        json.dumps(
            {
                "id": "hook-1",
                "request_type": "hook_send",
                "created_at": "2026-05-15T00:00:00+00:00",
                "session_id": "sesk8m4q2p7x",
                "session_key": "slack::channel::C123",
                "prompt": "queued",
            }
        ),
        encoding="utf-8",
    )
    completed = state_dir / "task_requests" / "completed"
    completed.mkdir(parents=True)
    (completed / "hook-2.json").write_text(
        json.dumps(
            {
                "id": "hook-2",
                "request_type": "hook_send",
                "created_at": "2026-05-15T00:00:00+00:00",
                "completed_at": "2026-05-15T00:01:00+00:00",
                "session_id": "sesk8m4q2p7x",
                "session_key": "slack::channel::C123",
                "prompt": "failed",
                "ok": False,
                "error": "boom",
            }
        ),
        encoding="utf-8",
    )

    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    assert report.counts["background_scheduled_tasks"] == 1
    assert report.counts["background_watches"] == 1
    assert report.counts["background_runs_imported"] == 2
    with sqlite3.connect(db_path) as conn:
        tasks = conn.execute(
            "select definition_type, session_id, legacy_session_key from run_definitions order by id"
        ).fetchall()
        runs = conn.execute("select id, run_type, status, session_id, error from agent_runs order by id").fetchall()
    assert tasks == [
        ("scheduled", "sesk8m4q2p7x", "slack::channel::C123"),
        ("watch", "sesk8m4q2p7x", "slack::channel::C123"),
    ]
    assert runs == [
        ("hook-1", "hook_send", "queued", "sesk8m4q2p7x", None),
        ("hook-2", "hook_send", "failed", "sesk8m4q2p7x", "boom"),
    ]


def test_custom_state_paths_do_not_bootstrap_default_home(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "isolated-state"

    def fail_default_bootstrap() -> None:
        raise AssertionError("default Vibe home should not be bootstrapped for custom state paths")

    monkeypatch.setattr(paths, "ensure_data_dirs", fail_default_bootstrap)

    report = ensure_sqlite_state(db_path=state_dir / "vibe.sqlite", state_dir=state_dir)

    assert report.imported is True
    assert (state_dir / "vibe.sqlite").exists()


def test_legacy_sessions_import_requires_platform_when_not_inferable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "session_mappings": {
                    "C123": {
                        "codex": {
                            "1774074591.762089:/repo": "codex-session-1",
                        }
                    }
                },
                "active_polls": {
                    "opencode-session-1": {
                        "opencode_session_id": "opencode-session-1",
                        "base_session_id": "base-1",
                        "channel_id": "C123",
                        "thread_id": "1774074591.762089",
                        "settings_key": "C123",
                        "working_path": "/repo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="primary_platform is required"):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir)

    with sqlite3.connect(db_path) as conn:
        marker = conn.execute(
            "select value_json from state_meta where key = 'json_import_completed_at'",
        ).fetchone()
    assert marker is None


def test_legacy_settings_import_does_not_rewrite_source_json(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    settings_path = state_dir / "settings.json"
    original = json.dumps(
        {
            "channels": {
                "C123": {
                    "enabled": True,
                    "show_message_types": ["assistant"],
                    "custom_cwd": "/repo",
                }
            }
        },
        indent=2,
    )
    settings_path.write_text(original, encoding="utf-8")

    report = ensure_sqlite_state(db_path=state_dir / "vibe.sqlite", state_dir=state_dir, primary_platform="slack")

    assert report.imported is True
    assert report.counts["scope_settings"] == 1
    assert settings_path.read_text(encoding="utf-8") == original


def test_failed_json_import_does_not_mark_complete_and_can_retry(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "settings.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        marker = conn.execute(
            "select value_json from state_meta where key = 'json_import_completed_at'",
        ).fetchone()
    assert marker is None

    _write_current_settings(state_dir / "settings.json")
    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    assert report.imported is True
    assert report.counts["scope_settings"] == 4


def test_invalid_discovered_chats_import_does_not_block_core_state_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    _write_current_settings(state_dir / "settings.json")
    _write_current_sessions(state_dir / "sessions.json")
    (state_dir / "discovered_chats.json").write_text("{not-json", encoding="utf-8")

    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        marker = conn.execute(
            "select value_json from state_meta where key = 'json_import_completed_at'",
        ).fetchone()

    assert report.imported is True
    assert marker is not None
    assert report.counts["scope_settings"] == 4
    assert report.counts["agent_sessions"] == 1
    assert report.counts["discovered_scopes"] == 0
    assert report.counts["discovered_chats_skipped"] == 1


def test_malformed_discovered_chats_structure_does_not_block_core_state_migration(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    _write_current_settings(state_dir / "settings.json")
    _write_current_sessions(state_dir / "sessions.json")
    (state_dir / "discovered_chats.json").write_text(
        json.dumps({"schema_version": 1, "platforms": {"telegram": ["not", "a", "map"]}}),
        encoding="utf-8",
    )

    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    with sqlite3.connect(db_path) as conn:
        marker = conn.execute(
            "select value_json from state_meta where key = 'json_import_completed_at'",
        ).fetchone()

    assert report.imported is True
    assert marker is not None
    assert report.counts["scope_settings"] == 4
    assert report.counts["agent_sessions"] == 1
    assert report.counts["discovered_scopes"] == 0
    assert report.counts["discovered_chats_skipped"] == 1


def test_data_version_probe_detects_external_write(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    try:
        with SqliteInvalidationProbe(engine) as probe:
            assert probe.has_external_write() is False
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "insert into state_meta (key, value_json, updated_at) values ('probe', '1', 'now')"
                )
            assert probe.has_external_write() is True
            assert probe.has_external_write() is False
    finally:
        engine.dispose()


def _write_current_settings(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "scopes": {
                    "channel": {
                        "slack": {
                            "C123": {
                                "enabled": True,
                                "show_message_types": ["assistant", "toolcall"],
                                "custom_cwd": "/repo",
                                "routing": {"agent_backend": "codex", "codex_model": "gpt-5.4"},
                                "require_mention": False,
                            }
                        }
                    },
                    "guild": {"discord": {"G123": {"enabled": True}}},
                    "guild_policy": {"discord": {"default_enabled": False}},
                    "user": {
                        "slack": {
                            "U123": {
                                "display_name": "Alex",
                                "is_admin": True,
                                "bound_at": "2026-05-01T00:00:00+00:00",
                                "enabled": True,
                                "show_message_types": ["assistant"],
                                "custom_cwd": "/repo",
                                "routing": {"agent_backend": "opencode"},
                                "dm_chat_id": "D123",
                            }
                        }
                    },
                },
                "bind_codes": [
                    {
                        "code": "vr-abc123",
                        "type": "one_time",
                        "created_at": "2026-05-01T00:00:00+00:00",
                        "expires_at": None,
                        "is_active": True,
                        "used_by": ["U123"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_current_sessions(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "session_mappings": {
                    "slack::C123": {
                        "codex": {
                            "slack_1774074591.762089:/repo": "codex-session-1",
                        }
                    }
                },
                "active_slack_threads": {
                    "slack::C123": {
                        "C123": {
                            "1774074591.762089": 1774074591.762089,
                        }
                    }
                },
                "active_polls": {
                    "opencode-session-1": {
                        "opencode_session_id": "opencode-session-1",
                        "base_session_id": "base-1",
                        "channel_id": "C123",
                        "thread_id": "1774074591.762089",
                        "settings_key": "C123",
                        "working_path": "/repo",
                        "baseline_message_ids": ["m0"],
                        "seen_tool_calls": ["tool-1"],
                        "emitted_assistant_messages": ["m1"],
                        "started_at": 1774074591.0,
                        "typing_indicator_active": True,
                        "context_token": "ctx",
                        "processing_indicator": {"platform": "slack"},
                        "user_id": "U123",
                        "platform": "slack",
                    }
                },
                "processed_message_ts": {
                    "C123": {
                        "1774074591.762089": ["m1", "m2"],
                    }
                },
                "last_activity": "2026-05-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def _write_discovered_chats(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platforms": {
                    "telegram": {
                        "123": {
                            "name": "General",
                            "username": "general",
                            "chat_type": "supergroup",
                            "is_private": False,
                            "is_forum": True,
                            "supports_topics": True,
                            "last_seen_at": "2026-05-01T00:00:00+00:00",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
