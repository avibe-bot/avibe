from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from importlib import import_module
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from types import MappingProxyType

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from config import paths
from config.v2_settings import ChannelSettings, RoutingSettings, SettingsState, SettingsStore
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.importer import JSON_IMPORT_MARKER, ensure_sqlite_state, reset_ensured_sqlite_state
from storage.lock import migration_lock_path_for
from storage import importer, message_deliveries, messages_service, migrations
from storage.background import SQLiteBackgroundTaskStore
from storage.migrations import UnsafeDefaultStateMigrationError, background_tables_ready, run_migrations
from storage.models import metadata
from storage.settings_service import SQLiteSettingsService, upsert_scope
from vibe.message_types import build_partial_index_predicate


pytestmark = pytest.mark.no_sqlite_template


HEAD_REVISION = "20260821_0060"
# ``storage.models`` builds a bare ``MetaData()``, so its foreign keys are unnamed and
# Alembic cannot re-emit them when batch mode recreates a table. Every migration that
# rebuilds one therefore passes this convention; the rebuild simulated below has to pass
# the same one to reproduce what those migrations actually do.
_MIGRATION_FK_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}
MESSAGE_PARTIAL_INDEX_PREDICATES = {
    "ix_messages_inbox_activity": (
        "session_id is not null and type in "
        "('user', 'harness', 'agent_initiated', 'annotation', 'output', "
        "'result', 'notify', 'vault', 'error', 'assistant')"
    ),
    "ix_messages_inbox_agent_reply": (
        "session_id is not null and type in ('output', 'result', 'notify', 'vault', 'error')"
    ),
    "ix_messages_inbox_user_send": (
        "session_id is not null and ((author = 'user' and type = 'user') "
        "or (author = 'harness' and type = 'harness') "
        "or (author = 'harness' and type = 'agent_initiated') "
        "or (author = 'harness' and type = 'annotation'))"
    ),
}


def test_local_show_access_migration_round_trip_preserves_pages_and_fails_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260815_0054")
    legacy_rows = [
        ("private-null", "private", None, None),
        ("private-stable", "private", "private-link", None),
        ("public-null", "public", None, None),
        ("public-stable", "public", "public-link", None),
        ("offline-null", "offline", None, None),
        ("offline-stable", "offline", "offline-link", "2026-08-16T00:00:00Z"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            insert into show_pages (
                session_id, visibility, share_id, offline_at, created_at, updated_at
            ) values (?, ?, ?, ?, '2026-08-15T00:00:00Z', '2026-08-16T00:00:00Z')
            """,
            legacy_rows,
        )

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("pragma foreign_keys = on")
        columns = {row[1] for row in conn.execute("pragma table_info(show_pages)")}
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "select session_id, access_mode, access_revision, share_id, offline_at "
                "from show_pages order by session_id"
            )
        }
        assert "visibility" not in columns
        assert {"access_mode", "access_revision", "share_id", "offline_at"}.issubset(columns)
        assert rows["private-null"][0:2] == ("private", 0)
        assert rows["private-stable"] == ("private", 0, "private-link", None)
        assert rows["public-null"][0:2] == ("public", 0)
        assert rows["public-stable"] == ("public", 0, "public-link", None)
        assert rows["offline-null"][0] == "private"
        assert rows["offline-null"][3] == "2026-08-16T00:00:00Z"
        assert rows["offline-stable"] == (
            "private",
            0,
            "offline-link",
            "2026-08-16T00:00:00Z",
        )
        assert all(row[2] for row in rows.values())

        show_indexes = {row[1] for row in conn.execute("pragma index_list(show_pages)")}
        # 20260820_0058 moved the audience out of ``show_page_authorized_emails``
        # into the heterogeneous entry table, so that is where the audience half
        # of this round trip lives at head.
        entry_indexes = {
            row[1]
            for row in conn.execute("pragma index_list(show_page_access_entries)")
        }
        assert {"ix_show_pages_share_id", "ix_show_pages_access_mode"}.issubset(show_indexes)
        assert {
            "ix_show_page_access_entries_lookup",
            "uq_show_page_access_entries_organization",
        }.issubset(entry_indexes)
        assert "show_page_authorized_emails" not in {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        foreign_key = conn.execute(
            "pragma foreign_key_list(show_page_access_entries)"
        ).fetchone()
        assert foreign_key is not None
        assert foreign_key[2] == "show_pages"
        assert foreign_key[6].upper() == "CASCADE"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "update show_pages set access_mode = 'other' where session_id = 'private-null'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "update show_pages set access_revision = -1 where session_id = 'private-null'"
            )

        conn.execute(
            "update show_pages set access_mode = 'limited', access_revision = 3 "
            "where session_id = 'private-stable'"
        )
        conn.execute(
            "insert into show_page_access_entries values (?, ?, ?, ?, ?)",
            ("private-stable", "email", "guest@example.com", None, "2026-08-17T00:00:00Z"),
        )
        conn.execute(
            "insert into show_page_access_entries values (?, ?, ?, ?, ?)",
            ("private-null", "email", "cascade@example.com", None, "2026-08-17T00:00:00Z"),
        )
        conn.execute("delete from show_pages where session_id = 'private-null'")
        assert conn.execute(
            "select count(*) from show_page_access_entries where page_id = 'private-null'"
        ).fetchone() == (0,)

    command.downgrade(migrations.alembic_config(db_path), "20260815_0054")
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(show_pages)")}
        downgraded = dict(
            conn.execute(
                "select session_id, visibility from show_pages order by session_id"
            )
        )
        retained_slugs = dict(
            conn.execute("select session_id, share_id from show_pages order by session_id")
        )
        assert "visibility" in columns
        assert "access_mode" not in columns
        assert "access_revision" not in columns
        assert "show_page_authorized_emails" not in {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        assert downgraded["private-stable"] == "private"
        assert downgraded["public-null"] == "public"
        assert downgraded["public-stable"] == "public"
        assert downgraded["offline-null"] == "offline"
        assert downgraded["offline-stable"] == "offline"
        assert retained_slugs["private-stable"] == "private-link"
        assert retained_slugs["public-stable"] == "public-link"
        assert retained_slugs["offline-stable"] == "offline-link"

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        reupgraded = conn.execute(
            "select access_mode, access_revision, share_id from show_pages "
            "where session_id = 'private-stable'"
        ).fetchone()
        entry_count = conn.execute(
            "select count(*) from show_page_access_entries"
        ).fetchone()
    assert reupgraded == ("private", 0, "private-link")
    assert entry_count == (0,)


SHOW_PAGE_ACCESS_ENTRY_MIGRATION_MODULE = (
    "storage.alembic.versions.20260820_0058_show_page_access_entries"
)


def _seed_show_pages(conn: sqlite3.Connection, session_ids: Iterable[str]) -> None:
    conn.executemany(
        """
        insert into show_pages (
            session_id, share_id, offline_at, access_mode, access_revision,
            created_at, updated_at
        ) values (?, ?, null, 'limited', 1, '2026-08-19T00:00:00Z', '2026-08-19T00:00:00Z')
        """,
        [(session_id, f"{session_id}-link") for session_id in session_ids],
    )


def _restore_legacy_show_page_email_table(db_path: Path) -> None:
    """Put the retired pre-0058 email table back the way a replay finds it.

    An unversioned database is stamped at the replay floor, so 20260820_0058
    runs again over state it already produced. Reusing the revision's own
    downgrade helper keeps this fixture from becoming a second, drifting copy of
    that table's shape.
    """

    migration = import_module(SHOW_PAGE_ACCESS_ENTRY_MIGRATION_MODULE)
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                migration._create_legacy_email_table()
    finally:
        engine.dispose()


def test_show_page_access_entry_migration_moves_every_email_row_and_narrows_on_downgrade(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260819_0057")
    # One row of every audience shape 20260819_0057 can hold: a page with
    # several addresses, a page with one, and a page with none.
    seeded_emails = [
        ("many-emails", "first@example.com", "2026-08-17T00:00:00Z"),
        ("many-emails", "second@example.com", "2026-08-18T00:00:00Z"),
        ("one-email", "only@example.com", "2026-08-18T12:00:00Z"),
    ]
    with sqlite3.connect(db_path) as conn:
        _seed_show_pages(conn, ("many-emails", "one-email", "no-emails"))
        conn.executemany(
            "insert into show_page_authorized_emails values (?, ?, ?)", seeded_emails
        )

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        migrated = conn.execute(
            "select page_id, kind, value, organization_id, created_at "
            "from show_page_access_entries order by page_id, kind, value"
        ).fetchall()
        # The organization-scoped kinds have no pre-0058 representation, so they
        # only exist from here on.
        conn.executemany(
            "insert into show_page_access_entries values (?, ?, ?, ?, ?)",
            [
                ("many-emails", "group", "group-7", "org-1", "2026-08-20T00:00:00Z"),
                ("many-emails", "organization", "org-1", "org-1", "2026-08-20T00:00:00Z"),
            ],
        )
    assert migrated == [
        (page_id, "email", value, None, created_at)
        for page_id, value, created_at in sorted(seeded_emails)
    ]

    command.downgrade(migrations.alembic_config(db_path), "20260819_0057")
    with sqlite3.connect(db_path) as conn:
        restored = conn.execute(
            "select session_id, normalized_email, created_at "
            "from show_page_authorized_emails order by session_id, normalized_email"
        ).fetchall()
        legacy_indexes = {
            row[1]
            for row in conn.execute("pragma index_list(show_page_authorized_emails)")
        }
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
    # A pre-0058 reader only understands emails, so the audience narrows and
    # fails closed: the group and organization grants are dropped, never widened
    # into an email that was never granted.
    assert restored == sorted(seeded_emails)
    assert "ix_show_page_authorized_emails_email" in legacy_indexes
    assert "show_page_access_entries" not in tables

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        reupgraded = conn.execute(
            "select page_id, kind, value, organization_id, created_at "
            "from show_page_access_entries order by page_id, kind, value"
        ).fetchall()
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
    assert reupgraded == [
        (page_id, "email", value, None, created_at)
        for page_id, value, created_at in sorted(seeded_emails)
    ]
    assert "show_page_authorized_emails" not in tables


def test_show_page_access_entry_migration_replay_neither_duplicates_nor_overwrites(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    already_migrated = [
        ("replayed", "email", "kept@example.com", None, "2026-08-20T00:00:00Z"),
        ("replayed", "group", "group-7", "org-1", "2026-08-20T00:00:00Z"),
    ]
    with sqlite3.connect(db_path) as conn:
        _seed_show_pages(conn, ("replayed",))
        conn.executemany(
            "insert into show_page_access_entries values (?, ?, ?, ?, ?)", already_migrated
        )

    _restore_legacy_show_page_email_table(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "insert into show_page_authorized_emails values (?, ?, ?)",
            [
                # One address this revision has already moved, carrying a
                # different timestamp, and one it has never seen.
                ("replayed", "kept@example.com", "2000-01-01T00:00:00Z"),
                ("replayed", "added@example.com", "2026-08-21T00:00:00Z"),
            ],
        )
    command.stamp(migrations.alembic_config(db_path), "20260819_0057")

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        entries = conn.execute(
            "select page_id, kind, value, organization_id, created_at "
            "from show_page_access_entries order by kind, value"
        ).fetchall()
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
    assert entries == [
        ("replayed", "email", "added@example.com", None, "2026-08-21T00:00:00Z"),
        *sorted(already_migrated, key=lambda row: (row[1], row[2])),
    ]
    assert "show_page_authorized_emails" not in tables


def test_show_page_access_entry_constraints_hold_for_every_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    insert = "insert into show_page_access_entries values (?, ?, ?, ?, '2026-08-20T00:00:00Z')"
    accepted = [
        ("page", "email", "guest@example.com", None),
        ("page", "group", "group-7", "org-1"),
        ("page", "group", "group-8", "org-1"),
        ("page", "organization", "org-1", "org-1"),
        # Another page's audience is independent, including its organization.
        ("other", "group", "group-7", "org-1"),
        ("other", "organization", "org-1", "org-1"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("pragma foreign_keys = on")
        _seed_show_pages(conn, ("page", "other"))
        conn.executemany(insert, accepted)

        for row in accepted:
            # Every accepted shape is unique per (page, kind, value)...
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, row)
        # ...and "this organization may read" is one switch per page, not a
        # list, which the composite key alone cannot say.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("page", "organization", "org-2", "org-2"))

        rejected = [
            ("page", "user", "someone", None),
            ("page", "email", "other@example.com", "org-1"),
            ("page", "group", "group-9", None),
            ("page", "organization", "org-9", "org-1"),
            ("page", "email", "", None),
            ("page", "email", "a" * 321, None),
            ("missing-page", "email", "guest@example.com", None),
        ]
        for row in rejected:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, row)

        conn.execute("delete from show_pages where session_id = 'other'")
        surviving = conn.execute(
            "select page_id, kind, value, organization_id "
            "from show_page_access_entries order by page_id, kind, value"
        ).fetchall()
    assert surviving == sorted(row for row in accepted if row[0] == "page")


def _index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("select sql from sqlite_master where type = 'index' and name = ?", (name,)).fetchone()
    assert row is not None
    return str(row[0] or "")


def test_remote_authorization_context_migration_preserves_legacy_rows_both_directions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260812_0053")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into remote_access_authorizations (
                id, instance_id, subject, claims_json, expires_at, created_at
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-auth-reference",
                "inst-1",
                "user-1",
                '{"legacy":true}',
                200,
                100,
            ),
        )

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        legacy = conn.execute(
            "select claims_json, expires_at, scope_kind from remote_access_authorizations "
            "where id = 'legacy-auth-reference'"
        ).fetchone()
        conn.execute(
            """
            insert into remote_access_authorizations (
                id, instance_id, subject, email, scope_kind, scope_ref,
                authorization_state, claims_json, expires_at, created_at,
                last_checked_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "scoped-auth-reference",
                "inst-1",
                "user-1",
                "user@example.com",
                "instance",
                "inst-1",
                "current",
                '{"current":true}',
                None,
                101,
                101,
                101,
            ),
        )
    assert legacy == ('{"legacy":true}', 200, None)

    command.downgrade(migrations.alembic_config(db_path), "20260812_0053")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select id, claims_json, expires_at from remote_access_authorizations"
        ).fetchall()
        expires_column = next(
            row for row in conn.execute("pragma table_info(remote_access_authorizations)")
            if row[1] == "expires_at"
        )
        columns = {row[1] for row in conn.execute("pragma table_info(remote_access_authorizations)")}

    assert rows == [("legacy-auth-reference", '{"legacy":true}', 200)]
    assert expires_column[3] == 1
    assert not {
        "email",
        "scope_kind",
        "scope_ref",
        "authorization_state",
        "last_checked_at",
        "updated_at",
    } & columns


@pytest.mark.parametrize(
    ("index_name", "expected_predicate"),
    tuple(MESSAGE_PARTIAL_INDEX_PREDICATES.items()),
)
def test_message_partial_index_ddl_matches_catalog_contract(
    index_name: str,
    expected_predicate: str,
) -> None:
    index = next(index for index in metadata.tables["messages"].indexes if index.name == index_name)

    catalog_predicate = build_partial_index_predicate(index_name)
    ddl = str(CreateIndex(index).compile(dialect=sqlite_dialect()))

    assert catalog_predicate == expected_predicate
    assert ddl == (
        f"CREATE INDEX {index_name} ON messages "
        "(platform, session_id, coalesce(delivered_at, created_at) desc, id desc) "
        f"WHERE {expected_predicate}"
    )


def test_message_index_migration_matches_catalog() -> None:
    migration = import_module(
        "storage.alembic.versions.20260809_0049_vault_message_type"
    )

    assert migration.UPGRADE_ACTIVITY_PREDICATE == build_partial_index_predicate(
        "ix_messages_inbox_activity"
    )
    assert migration.UPGRADE_AGENT_REPLY_PREDICATE == build_partial_index_predicate(
        "ix_messages_inbox_agent_reply"
    )
    assert migration.UPGRADE_USER_SEND_PREDICATE == build_partial_index_predicate(
        "ix_messages_inbox_user_send"
    )


def test_accepted_steer_receipt_migration_preserves_upgrade_and_downgrade_invariants(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260809_0049")
    engine = create_sqlite_engine(db_path)
    now = "2026-08-11T00:00:00Z"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            insert into agent_sessions (
                id, scope_id, agent_name, agent_backend, agent_variant,
                session_anchor, workdir, native_session_id, status, visibility,
                pinned, agent_status, metadata_json, created_at, updated_at,
                last_active_at
            ) values (
                'ses_receipt_migration', null, 'codex', 'codex', 'codex',
                'receipt-migration', '/tmp', '', 'active', 'foreground',
                0, 'idle', '{}', ?, ?, ?
            )
            """,
            (now, now, now),
        )
        initial = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_receipt_anchor",
            session_id="ses_receipt_migration",
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=None,
                session_id="ses_receipt_migration",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="anchor",
            ),
            dispatch_text="anchor",
            now=now,
        )
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id="turn_receipt_migration",
            session_id="ses_receipt_migration",
            backend="codex",
            deliveries=[initial],
            dispatch_text="anchor",
        )
        turn = message_deliveries.bind_native_start(
            conn,
            "turn_receipt_migration",
            expected_version=int(claimed["turn"]["version"]),
            runtime_key="runtime",
            runtime_turn_id="runtime-turn",
            native_turn_id="native-turn",
        )
        assert turn is not None
        steer = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_receipt_candidate",
            session_id="ses_receipt_migration",
            priority="p1",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=None,
                session_id="ses_receipt_migration",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="candidate",
            ),
            dispatch_text="candidate",
            now=now,
        )
        steering = message_deliveries.open_steer_attempt(
            conn,
            "msg_receipt_candidate",
            expected_version=int(steer["version"]),
            turn_id="turn_receipt_migration",
            attempt_id="attempt_receipt_migration",
            expected_native_turn_id="native-turn",
        )
        assert steering is not None
        assert message_deliveries.mark_attempt_unknown(
            conn,
            "msg_receipt_candidate",
            expected_version=int(steering["version"]),
            receipt={"reason": "receipt_persistence_lost"},
        ) is not None

    command.upgrade(migrations.alembic_config(db_path), "head")
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "select state, current_receipt_outcome, current_receipt_json "
            "from message_deliveries where id = 'msg_receipt_candidate'"
        ).one() == (
            "reconciling_steer",
            "unknown",
            '{"reason":"receipt_persistence_lost"}',
        )
    with engine.begin() as conn:
        conn.execute(
            message_deliveries.message_deliveries.update()
            .where(message_deliveries.message_deliveries.c.id == "msg_receipt_candidate")
            .values(
                current_receipt_outcome="accepted",
                current_receipt_json='{"reason":"native-accepted"}',
            )
        )
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "select state, current_receipt_outcome from message_deliveries "
            "where id = 'msg_receipt_candidate'"
        ).one() == ("reconciling_steer", "accepted")

    with pytest.raises(RuntimeError, match="0050 downgrade refused"):
        command.downgrade(migrations.alembic_config(db_path), "20260809_0049")

    with engine.begin() as conn:
        conn.execute(
            message_deliveries.message_deliveries.update()
            .where(message_deliveries.message_deliveries.c.id == "msg_receipt_candidate")
            .values(
                current_receipt_outcome="unknown",
                current_receipt_json='{"reason":"receipt_persistence_lost"}',
            )
        )
    command.downgrade(migrations.alembic_config(db_path), "20260809_0049")
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "select state, current_receipt_outcome, current_receipt_json "
            "from message_deliveries where id = 'msg_receipt_candidate'"
        ).one() == (
            "reconciling_steer",
            "unknown",
            '{"reason":"receipt_persistence_lost"}',
        )
        assert conn.exec_driver_sql("select version_num from alembic_version").one() == (
            "20260809_0049",
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "update message_deliveries set current_receipt_outcome = 'accepted' "
                "where id = 'msg_receipt_candidate'"
            )


def test_callback_terminal_turn_identity_migration_preserves_rows_and_scope(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260811_0050")
    command.upgrade(migrations.alembic_config(db_path), "head")
    now = "2026-08-11T00:00:00Z"

    def insert_callback(
        conn,
        *,
        run_id: str,
        terminal_turn_id: str,
        session_id: str,
    ) -> None:
        conn.execute(
            """
            insert into agent_runs (
                id, run_type, status, source_kind, session_id,
                callback_terminal_turn_id, cancel_requested,
                created_at, updated_at, metadata_json
            ) values (?, 'agent_run', 'queued', 'callback', ?, ?, 0, ?, ?, '{}')
            """,
            (run_id, session_id, terminal_turn_id, now, now),
        )

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info('agent_runs')")}
        assert "callback_terminal_turn_id" in columns
        assert "where run_type = 'agent_run'" in _index_sql(
            conn, "uq_agent_runs_callback_terminal_turn_session"
        ).lower()
        insert_callback(
            conn,
            run_id="callback-one",
            terminal_turn_id="turn-one",
            session_id="session-one",
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(db_path) as conn:
            insert_callback(
                conn,
                run_id="callback-duplicate",
                terminal_turn_id="turn-one",
                session_id="session-one",
            )

    with sqlite3.connect(db_path) as conn:
        insert_callback(
            conn,
            run_id="callback-other-turn",
            terminal_turn_id="turn-two",
            session_id="session-one",
        )
        insert_callback(
            conn,
            run_id="callback-other-session",
            terminal_turn_id="turn-one",
            session_id="session-two",
        )
        conn.commit()

    command.downgrade(migrations.alembic_config(db_path), "20260811_0050")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select name from sqlite_master where type = 'index' and name = ?",
            ("uq_agent_runs_callback_terminal_turn_session",),
        ).fetchone() is None
        assert conn.execute(
            "select id from agent_runs where source_kind = 'callback' order by id"
        ).fetchall() == [
            ("callback-one",),
            ("callback-other-session",),
            ("callback-other-turn",),
        ]


def test_agent_lifecycle_message_index_migration_upgrades_and_downgrades(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260806_0047")

    with sqlite3.connect(db_path) as conn:
        previous_activity = _index_sql(conn, "ix_messages_inbox_activity")
        previous_agent_reply = _index_sql(conn, "ix_messages_inbox_agent_reply")
        previous_user_send = _index_sql(conn, "ix_messages_inbox_user_send")
        conn.execute(
            "insert into messages "
            "(id, platform, author, type, content_json, metadata_json, created_at, updated_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-vault-waiter",
                "avibe",
                "harness",
                "notify",
                '{"text":"legacy Vault result"}',
                '{"source_kind":"callback","source_actor":"vault:vrq_legacy"}',
                "2026-08-09T00:00:00.000000Z",
                "2026-08-09T00:00:00.000000Z",
            ),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        assert _index_sql(conn, "ix_messages_inbox_activity").endswith(
            MESSAGE_PARTIAL_INDEX_PREDICATES["ix_messages_inbox_activity"]
        )
        assert _index_sql(conn, "ix_messages_inbox_user_send").endswith(
            MESSAGE_PARTIAL_INDEX_PREDICATES["ix_messages_inbox_user_send"]
        )
        assert _index_sql(conn, "ix_messages_inbox_agent_reply").endswith(
            MESSAGE_PARTIAL_INDEX_PREDICATES["ix_messages_inbox_agent_reply"]
        )
        assert conn.execute("select version_num from alembic_version").fetchone() == (
            HEAD_REVISION,
        )
        assert conn.execute(
            "select type from messages where id = ?", ("legacy-vault-waiter",)
        ).fetchone() == ("vault",)

    command.downgrade(migrations.alembic_config(db_path), "20260806_0047")

    with sqlite3.connect(db_path) as conn:
        assert _index_sql(conn, "ix_messages_inbox_activity") == previous_activity
        assert _index_sql(conn, "ix_messages_inbox_agent_reply") == previous_agent_reply
        assert _index_sql(conn, "ix_messages_inbox_user_send") == previous_user_send
        assert conn.execute(
            "select type from messages where id = ?", ("legacy-vault-waiter",)
        ).fetchone() == ("notify",)


class _Pre335Cursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).upper().split())
        if " DROP COLUMN " in f" {normalized} ":
            raise sqlite3.OperationalError("near \"DROP\": syntax error")
        return super().execute(sql, parameters)


class _Pre335Connection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _Pre335Cursor)


def test_alembic_script_directory_has_exactly_one_head() -> None:
    heads = ScriptDirectory.from_config(migrations.alembic_config()).get_heads()

    assert len(heads) == 1
    assert heads[0] == HEAD_REVISION


# v3.0.11 added a branch under 20260724_0034 plus the 20260804_0047 merge, and
# repointed 20260806_0047 from 20260804_0046 onto that merge. 20260806_0047 shipped
# in v3.0.9, so every database already live had passed it. Alembic only walks
# forward from the revision a database is on and never applies an ancestor
# inserted behind it, so the whole branch was recorded as applied and none of its
# tables were ever created.
_SPLICE_POINT_REVISION = "20260806_0047"
_SPLICED_MERGE_REVISION = "20260804_0047"
_REPAIR_REVISION = "20260819_0056"
# Created by 20260725_0038 on the spliced branch and widened by 20260815_0054, so a
# replay interrupted between the two leaves it present and short of head.
_INTERRUPTED_TABLE = "remote_access_authorizations"


def _schema_of(db_path: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as conn:
        return {
            (str(kind), str(name), re.sub(r"\s+", " ", str(sql or "")).strip())
            for kind, name, sql in conn.execute(
                "select type, name, sql from sqlite_master "
                "where name not like 'sqlite_%' and name != 'alembic_version'"
            )
        }


def _upgraded_db(db_path: Path, revision: str) -> Path:
    command.upgrade(migrations.alembic_config(db_path), revision)
    return db_path


@pytest.fixture(scope="module")
def reference_schema(tmp_path_factory):
    references = {}
    root = tmp_path_factory.mktemp("migration-reference-schemas")

    def schema(revision: str):
        if revision not in references:
            path = _upgraded_db(root / f"reference-{len(references)}.sqlite", revision)
            references[revision] = frozenset(_schema_of(path))
        return references[revision]

    return schema


def test_schema_reference_is_immutable_and_built_once(reference_schema):
    reference = reference_schema("head")
    assert isinstance(reference, frozenset)
    assert reference_schema("head") is reference
    assert reference
    with pytest.raises(AttributeError):
        reference.clear()


def _revisions_the_repair_must_cover() -> list[str]:
    """Every released revision a database can still be sitting on below the repair.

    Read from the script directory rather than written down, so a revision added
    after this one is covered without editing the test.
    """

    script = ScriptDirectory.from_config(migrations.alembic_config())
    return [
        revision.revision
        for revision in script.iterate_revisions(
            _REPAIR_REVISION, _SPLICE_POINT_REVISION, inclusive=True
        )
        if revision.revision != _REPAIR_REVISION
    ]


def test_repair_completes_a_replay_interrupted_between_two_branch_revisions(
    tmp_path: Path, reference_schema,
) -> None:
    # Every branch table being present does not mean the branch was applied. A repair
    # interrupted after 20260725_0038 recreated the table but before 20260815_0054
    # replayed leaves all six tables there, that one short of head, and Alembic still
    # stamped below the repair. Skipping the replay on a table-presence check would
    # stamp the repair over the short table, and a stamped revision never runs again:
    # the columns would then be missing for the life of the database.
    expected = {obj for obj in reference_schema("head") if obj[1] == _INTERRUPTED_TABLE}
    assert expected, "the interrupted table must exist at head"

    # Take the interrupted shape from the revision that creates it instead of writing
    # its DDL down here, so this stays the real pre-20260815_0054 table.
    scratch = _upgraded_db(tmp_path / "scratch.sqlite", _SPLICED_MERGE_REVISION)
    with sqlite3.connect(scratch) as conn:
        interrupted_ddl = [
            str(sql)
            for (sql,) in conn.execute(
                "select sql from sqlite_master where tbl_name = ? and sql is not null",
                (_INTERRUPTED_TABLE,),
            )
        ]
    assert interrupted_ddl

    db_path = _upgraded_db(tmp_path / "interrupted.sqlite", "20260817_0055")
    with sqlite3.connect(db_path) as conn:
        conn.execute("pragma foreign_keys = off")
        conn.execute(f'drop table "{_INTERRUPTED_TABLE}"')
        for statement in interrupted_ddl:
            conn.execute(statement)
        conn.commit()
    assert not expected <= _schema_of(db_path), "the seeded database must really be short of head"

    command.upgrade(migrations.alembic_config(db_path), "head")

    missing = expected - _schema_of(db_path)
    assert not missing, f"the repair left an interrupted table short of head: {sorted(missing)}"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchall() == [
            (HEAD_REVISION,)
        ]


def test_repair_restores_branch_indexes_whose_tables_survived(tmp_path: Path, reference_schema) -> None:
    # A table is not the unit of interruption; a schema object is. Each branch
    # revision creates its table and then its index as two statements, so a run
    # interrupted between them leaves the table present and the index missing --
    # and a guard covering both reads the table as proof that neither is needed.
    # That state is exactly the one the repair exists to fix, and it is also the
    # one where skipping is permanent: the repair stamps, and a stamped revision
    # never runs again. Derive the objects from the two sides of the merge rather
    # than naming them, so an index added to the branch later is covered here
    # without editing the test.
    reference = reference_schema("head")
    without_branch = reference_schema("20260804_0046")
    with_branch = reference_schema(_SPLICED_MERGE_REVISION)
    branch_names = {name for _, name, _ in with_branch} - {name for _, name, _ in without_branch}
    branch_indexes = {name for kind, name, _ in with_branch if kind == "index"} & branch_names
    assert branch_indexes, "the spliced branch must own at least one index"
    expected = {obj for obj in reference if obj[1] in branch_names}

    db_path = _upgraded_db(tmp_path / "indexless.sqlite", "20260817_0055")
    with sqlite3.connect(db_path) as conn:
        for name in sorted(branch_indexes):
            conn.execute(f'drop index "{name}"')
        conn.commit()
    surviving = {name for _, name, _ in _schema_of(db_path)}
    assert not branch_indexes & surviving, "the seeded database must really be missing the indexes"
    assert {name for kind, name, _ in with_branch if kind == "table"} & branch_names <= surviving, (
        "only the indexes may be missing -- a dropped table would let the table guard "
        "recreate the index and the test would pass without proving anything"
    )

    command.upgrade(migrations.alembic_config(db_path), "head")

    missing = expected - _schema_of(db_path)
    assert not missing, f"the repair left branch objects short of a fresh install: {sorted(missing)}"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchall() == [
            (HEAD_REVISION,)
        ]


def test_replaying_the_branch_over_a_healthy_database_changes_nothing(tmp_path: Path) -> None:
    # The repair replays unconditionally, so every database that reaches it replays the
    # branch over a schema that already has it -- including the backfill in
    # 20260725_0037, which writes rows rather than DDL. That is only safe while every
    # replayed revision is a no-op against what it finds, so pin exactly that: same
    # schema, and a row the backfill would otherwise duplicate or overwrite left alone.
    #
    # Upgrade to the repair itself rather than to head. The subject here is the replay,
    # and a later revision is free to change the schema deliberately -- as 20260819_0057
    # does -- which would otherwise read as the replay having damaged something.
    db_path = _upgraded_db(tmp_path / "healthy.sqlite", "20260817_0055")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "insert into media_objects (token, session_id, kind, source, local_path, created_at) "
            "values ('tok', 'ses', 'image', 'agent', '/tmp/tok.png', 'created')"
        )
        conn.execute(
            "insert into media_object_references (token, session_id, created_at) "
            "values ('tok', 'ses', 'original')"
        )
        conn.commit()
    before = _schema_of(db_path)

    command.upgrade(migrations.alembic_config(db_path), _REPAIR_REVISION)

    assert _schema_of(db_path) == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select token, session_id, created_at from media_object_references"
        ).fetchall() == [("tok", "ses", "original")]
        assert conn.execute("select version_num from alembic_version").fetchall() == [
            (_REPAIR_REVISION,)
        ]


def test_repair_refuses_to_stamp_a_schema_it_could_not_restore(tmp_path: Path) -> None:
    # Two of the replayed revisions return silently when a table they reference is
    # absent, so "the replay ran" does not mean "the schema is repaired". The repair
    # must fail rather than record itself as applied over a schema that is still
    # short: a stamped half-repair can never re-run, and resurfaces later as an
    # unattributable error at whichever call site touches the missing table.
    db_path = _upgraded_db(tmp_path / "short.sqlite", "20260817_0055")
    with sqlite3.connect(db_path) as conn:
        conn.execute("pragma foreign_keys = off")
        for table in ("media_object_references", "media_objects"):
            conn.execute(f'drop table if exists "{table}"')
        conn.commit()

    with pytest.raises(RuntimeError, match="left these tables missing: media_object_references"):
        command.upgrade(migrations.alembic_config(db_path), "head")

    with sqlite3.connect(db_path) as conn:
        # Still below head, so the next upgrade retries the repair instead of
        # skipping it forever.
        assert conn.execute("select version_num from alembic_version").fetchall() == [
            ("20260817_0055",)
        ]


def test_spliced_branch_schema_is_restored_from_every_released_revision(tmp_path: Path, reference_schema) -> None:
    # The property: whatever released revision a database is on, upgrading to head
    # leaves every schema object the spliced branch owns in the shape a fresh install
    # has. Deriving that set from the two sides of the merge, instead of sharing a
    # hand-written list with the migration, means a table added to the branch later
    # cannot fall out of both at once.
    reference = reference_schema("head")
    without_branch = reference_schema("20260804_0046")
    with_branch = reference_schema(_SPLICED_MERGE_REVISION)

    # Compare by name: an object's recorded DDL text also varies with the rebuild
    # path a table took, which says nothing about who created it.
    branch_names = {name for _, name, _ in with_branch} - {name for _, name, _ in without_branch}
    branch_tables = {name for kind, name, _ in with_branch if kind == "table"} & branch_names
    expected = {obj for obj in reference if obj[1] in branch_names}
    assert branch_tables, "the spliced branch must own at least one table"
    assert expected, "the branch's objects must survive to head"

    # The repair's postcondition reads a literal tuple, because a migration cannot
    # learn what its replayed revisions create without running them. Pin that tuple to
    # two derivations it does not share: the merge boundary above, and the head table
    # set the rest of the codebase maintains. A table added to the branch later then
    # fails here, instead of slipping past the postcondition meant to catch it.
    script = ScriptDirectory.from_config(migrations.alembic_config())
    declared = set(script.get_revision(_REPAIR_REVISION).module._BRANCH_TABLES)
    assert declared == branch_tables
    assert declared <= migrations.HEAD_TABLES

    revisions = _revisions_the_repair_must_cover()
    assert _SPLICE_POINT_REVISION in revisions

    for seeded in revisions:
        db_path = tmp_path / f"stuck-{seeded}.sqlite"
        _upgraded_db(db_path, seeded)
        # Reproduce the splice rather than the fork: this database reached its
        # revision under a release where the branch did not exist at all.
        with sqlite3.connect(db_path) as conn:
            conn.execute("pragma foreign_keys = off")
            for table in branch_tables:
                conn.execute(f'drop table if exists "{table}"')
            conn.execute(
                "insert into state_meta (key, value_json, updated_at) "
                "values ('default_agent_name', '\"kept\"', 'before-upgrade')"
            )
            conn.commit()

        command.upgrade(migrations.alembic_config(db_path), "head")

        missing = expected - _schema_of(db_path)
        assert not missing, f"repair left {seeded} short of a fresh install: {sorted(missing)}"
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "select value_json from state_meta where key = 'default_agent_name'"
            ).fetchone() == ('"kept"',)
            assert conn.execute("select version_num from alembic_version").fetchall() == [
                (HEAD_REVISION,)
            ]


def test_message_transcript_order_upgrade_normalizes_and_indexes_exact_time(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260802_0045")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into messages (
                id, platform, author, type, content_json, metadata_json,
                created_at, updated_at, delivered_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg_legacy_order",
                "avibe",
                "user",
                "user",
                "{}",
                "{}",
                "2026-08-04T08:00:00+08:00",
                "2026-08-04T08:00:00+08:00",
                "2026-08-04T08:00:00.000999+08:00",
            ),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select created_at, delivered_at from messages where id = ?",
            ("msg_legacy_order",),
        ).fetchone()
        index_sql = _index_sql(conn, "ix_messages_session_transcript_id")
        dependent_index_sql = {
            name: _index_sql(conn, name)
            for name in (
                "ix_messages_mark_read",
                "ix_messages_inbox_activity",
                "ix_messages_inbox_agent_reply",
                "ix_messages_inbox_user_send",
            )
        }

    assert row == (
        "2026-08-04T00:00:00.000000Z",
        "2026-08-04T00:00:00.000999Z",
    )
    assert "coalesce(delivered_at, created_at)" in index_sql.lower()
    assert all(
        "coalesce(delivered_at, created_at)" in sql.lower()
        for sql in dependent_index_sql.values()
    )


def test_session_queue_hold_removal_is_schema_complete_and_reversible(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260804_0046")
    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "scope_queue_hold")
        _insert_agent_session(
            conn,
            row_id="ses_queue_hold",
            scope_id="scope_queue_hold",
            anchor="queue-hold",
            workdir=None,
            backend="codex",
            native="native-queue-hold",
            last_active="now",
        )
        conn.execute(
            "update agent_sessions set queue_hold_state='held', "
            "queue_hold_version=7, queue_held_at='now' where id='ses_queue_hold'"
        )
        conn.commit()

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(agent_sessions)")}
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert not {"queue_hold_state", "queue_hold_version", "queue_held_at"} & columns
    assert version == (HEAD_REVISION,)

    command.downgrade(migrations.alembic_config(db_path), "20260804_0046")
    with sqlite3.connect(db_path) as conn:
        restored = conn.execute(
            "select queue_hold_state, queue_hold_version, queue_held_at "
            "from agent_sessions where id='ses_queue_hold'"
        ).fetchone()
    assert restored == ("open", 1, None)


def _column_defaults(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Every stored column default in the database, keyed by (table, column)."""
    tables = [
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        )
    ]
    return {
        (table, row[1]): row[4]
        for table in tables
        for row in conn.execute(f"pragma table_info('{table}')")
        if row[4] is not None
    }


def _rebuild_every_table(db_path: Path) -> None:
    """Run the no-op batch rebuild Alembic performs for a SQLite table alteration.

    This leaves the database only good enough to read column defaults back from: batch
    mode cannot reflect an expression-based index, so rebuilding every table drops the
    partial and expression indexes this schema uses. Do not reuse it to assert anything
    about indexes.
    """
    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            operations = Operations(MigrationContext.configure(conn))
            tables = [
                row[0]
                for row in conn.exec_driver_sql(
                    "select name from sqlite_master where type = 'table' "
                    "and name not like 'sqlite_%' and name <> 'alembic_version'"
                )
            ]
            for table in tables:
                with operations.batch_alter_table(
                    table,
                    recreate="always",
                    naming_convention=_MIGRATION_FK_NAMING_CONVENTION,
                ):
                    pass
            conn.commit()
    finally:
        engine.dispose()


@pytest.mark.parametrize("schema", ["declared", "migrated"])
def test_no_table_rebuild_can_corrupt_a_column_default(tmp_path: Path, schema: str) -> None:
    """A table rebuild must preserve every column default, in every table.

    Alembic alters a SQLite column by reflecting the table and recreating it, and
    reflection hands a server default back as a ``TextClause``. Recompiling one
    re-reads ``:name`` as a bind parameter, so a default containing a colon is
    rewritten -- that is how ``20260811_0050`` silently turned
    ``message_deliveries.delivery_history_json`` into invalid JSON while replacing
    an unrelated check constraint.

    Asserting over every default in the schema is what makes this durable: the
    property holds for defaults nobody has written yet, so a colon-bearing default
    added to any table fails here when it is declared rather than one unrelated
    rebuild later. Both the declared schema and the migrated one are checked
    because they are separately authored and can disagree.
    """
    db_path = tmp_path / "vibe.sqlite"
    if schema == "declared":
        engine = create_sqlite_engine(db_path)
        try:
            metadata.create_all(engine)
        finally:
            engine.dispose()
    else:
        run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        before = _column_defaults(conn)
    assert before, "expected the schema under test to declare column defaults"

    _rebuild_every_table(db_path)

    with sqlite3.connect(db_path) as conn:
        after = _column_defaults(conn)
    drifted = {
        key: (before.get(key), after.get(key))
        for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    }
    assert drifted == {}


def test_head_column_defaults_satisfy_their_own_table_constraints(tmp_path: Path) -> None:
    """A defaulted column must accept an insert that omits it.

    The whole point of a server default is that a writer may leave the column out,
    so a default the table's own constraints reject is a broken column. This is the
    reachable half of the ``delivery_history_json`` defect: the corrupted default was
    not valid JSON, and ``ck_message_deliveries_history_json`` refused every insert
    that relied on it.
    """
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "scope_defaults")
        _insert_agent_session(
            conn,
            row_id="ses_defaults",
            scope_id="scope_defaults",
            anchor="defaults",
            workdir=None,
            backend="codex",
            native="native-defaults",
            last_active="now",
        )
        conn.execute(
            "insert into message_deliveries ("
            "id, session_id, priority, state, snapshot_sha256, dispatch_sha256, "
            "submitted_at, updated_at"
            ") values ('md_defaults', 'ses_defaults', 'p1', 'queued', 'h', 'h', 'now', 'now')"
        )
        stored = conn.execute(
            "select delivery_history_json from message_deliveries where id = 'md_defaults'"
        ).fetchone()[0]
        conn.commit()

    assert json.loads(stored) == {"version": 1, "events": []}


def test_delivery_history_default_repair_preserves_rows_and_reverses(tmp_path: Path) -> None:
    """0057 changes that one default and nothing else, over data and back.

    The repair rebuilds a table four others reference, so the rows that already
    exist are the thing at risk. One row of each shape a real database can hold is
    seeded -- a reference-clean row, and one whose parent is missing, because SQLite
    enforces foreign keys only when a connection asks it to and ``20260819_0056``
    exists to repair databases that are already damaged. Refusing to upgrade over
    pre-existing damage would pin such an install below head for a reason this
    revision did not cause.
    """
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260819_0056")

    history = json.dumps({"version": 1, "events": [{"kind": "queued"}]})
    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "scope_history")
        _insert_agent_session(
            conn,
            row_id="ses_history",
            scope_id="scope_history",
            anchor="history",
            workdir=None,
            backend="codex",
            native="native-history",
            last_active="now",
        )
        for row_id, session_id in (("md_clean", "ses_history"), ("md_orphan", "ses_missing")):
            conn.execute(
                "insert into message_deliveries ("
                "id, session_id, priority, state, snapshot_sha256, dispatch_sha256, "
                "submitted_at, updated_at, delivery_history_json"
                ") values (?, ?, 'p1', 'queued', 'h', 'h', 'now', 'now', ?)",
                (row_id, session_id, history),
            )
        conn.commit()
        seeded = _schema_fingerprint(conn)
        rows_before = _delivery_rows(conn)

    # The check the corrupted default violates is the reason this repair exists, so
    # prove the fingerprint actually captured it: a comparison over a set that silently
    # came out empty would hold no matter what the rebuild did.
    assert "ck_message_deliveries_history_json" in " ".join(
        seeded["constraints"]["message_deliveries"]
    )

    run_migrations(db_path, revision="20260819_0057")
    with sqlite3.connect(db_path) as conn:
        upgraded = _schema_fingerprint(conn)
        assert _delivery_rows(conn) == rows_before
        assert conn.execute("select version_num from alembic_version").fetchone() == (
            "20260819_0057",
        )
    # Only that one column's default may differ -- every other column, constraint,
    # index, and table in the database is untouched. Later revisions (0058+) are
    # a different change and have their own round-trip tests; pinning here keeps
    # 0057's invariant from absorbing them.
    assert _fingerprint_difference(seeded, upgraded) == {
        ("message_deliveries", "delivery_history_json")
    }

    command.downgrade(migrations.alembic_config(db_path), "20260819_0056")
    with sqlite3.connect(db_path) as conn:
        assert _schema_fingerprint(conn) == seeded
        assert _delivery_rows(conn) == rows_before


def _delivery_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "select id, session_id, delivery_history_json from message_deliveries order by id"
    ).fetchall()


def _schema_fingerprint(conn: sqlite3.Connection) -> dict[str, object]:
    """Column shapes, table constraints, indexes, and foreign keys, whole database.

    Constraints are compared as sets rather than as DDL text because a batch rebuild
    re-emits them in reflection order, which is not the order they were written in.
    """
    tables = sorted(
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
        )
    )
    columns = {
        (table, row[1]): tuple(row[2:6])
        for table in tables
        for row in conn.execute(f"pragma table_info('{table}')")
    }
    constraints = {
        table: {
            re.sub(r"\s+", " ", item)
            for item in _split_table_constraints(
                conn.execute(
                    "select sql from sqlite_master where type = 'table' and name = ?",
                    (table,),
                ).fetchone()[0]
            )
        }
        for table in tables
    }
    others = {
        (row[0], row[1]): re.sub(r"\s+", " ", row[2] or "")
        for row in conn.execute(
            "select type, name, sql from sqlite_master "
            "where type <> 'table' and name not like 'sqlite_%'"
        )
    }
    foreign_keys = {
        table: sorted(tuple(row[2:]) for row in conn.execute(f"pragma foreign_key_list('{table}')"))
        for table in tables
    }
    return {
        "columns": columns,
        "constraints": constraints,
        "others": others,
        "foreign_keys": foreign_keys,
    }


_TABLE_CONSTRAINT_KEYWORDS = ("constraint", "check", "foreign", "primary", "unique")


def _split_table_constraints(sql: str) -> list[str]:
    """Table-level constraints declared in a CREATE TABLE body.

    Column definitions are deliberately dropped: ``pragma table_info`` reports column
    shape exactly, so parsing it out of the DDL text again would only add a second,
    weaker reading of the same fact. A plain ``split(",")`` also cannot do this --
    ``check (state in ('a','b'))`` puts commas inside parentheses and a default like
    ``'{"a":1,"b":2}'`` puts one inside a string literal -- so both are tracked.
    """
    body = sql[sql.index("(") + 1 : sql.rindex(")")]
    items: list[str] = []
    depth = 0
    quoted = False
    current: list[str] = []
    for char in body:
        if char == "'":
            # SQLite escapes a quote by doubling it, which this toggle handles: the
            # second quote of '' re-enters the literal.
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        if char == "," and depth == 0 and not quoted:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return [
        item
        for item in items
        if item.split(" ", 1)[0].lower() in _TABLE_CONSTRAINT_KEYWORDS
    ]


def _fingerprint_difference(
    before: dict[str, object], after: dict[str, object]
) -> set[tuple[str, str]]:
    """(table, column) pairs whose shape changed; raises if anything else did."""
    for key in ("constraints", "others", "foreign_keys"):
        assert before[key] == after[key], f"{key} changed"
    before_columns = before["columns"]
    after_columns = after["columns"]
    assert before_columns.keys() == after_columns.keys()
    return {key for key in before_columns if before_columns[key] != after_columns[key]}


def test_scoped_native_message_identity_upgrade_and_safe_downgrade(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260801_0044")
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        now = messages_service._utc_now_iso()
        first_scope = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="chat-1",
            now=now,
        )
        messages_service.append(
            conn,
            scope_id=first_scope,
            session_id=None,
            platform="telegram",
            author="user",
            text="first",
            native_message_id="1",
        )

    command.upgrade(migrations.alembic_config(db_path), "head")

    with engine.begin() as conn:
        now = messages_service._utc_now_iso()
        second_scope = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="chat-2",
            now=now,
        )
        messages_service.append(
            conn,
            scope_id=second_scope,
            session_id=None,
            platform="telegram",
            author="user",
            text="second",
            native_message_id="1",
        )
        indexes = {
            row[1]
            for row in conn.exec_driver_sql("pragma index_list(messages)")
        }
    assert "uq_messages_platform_scope_native" in indexes
    assert "uq_messages_platform_native_unscoped" in indexes

    with pytest.raises(IntegrityError), engine.begin() as conn:
        messages_service.append(
            conn,
            scope_id=second_scope,
            session_id=None,
            platform="telegram",
            author="user",
            text="duplicate",
            native_message_id="1",
        )

    with pytest.raises(
        RuntimeError,
        match="conversation-scoped Message identities would collide",
    ):
        command.downgrade(migrations.alembic_config(db_path), "20260801_0044")


def test_scoped_native_message_identity_upgrade_preserves_accepted_delivery(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260801_0044")
    engine = create_sqlite_engine(db_path)
    now = "2026-08-02T00:00:00Z"
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="telegram",
            scope_type="channel",
            native_id="chat-accepted",
            now=now,
        )
        conn.exec_driver_sql(
            """
            insert into agent_sessions (
                id, scope_id, agent_name, agent_backend, agent_variant,
                session_anchor, workdir, native_session_id, status, visibility,
                pinned, agent_status, metadata_json, created_at, updated_at,
                last_active_at
            ) values (
                'ses_accepted', ?, 'codex', 'codex', 'codex', 'accepted',
                '/tmp', '', 'active', 'foreground', 0, 'idle', '{}', ?, ?, ?
            )
            """,
            (scope_id, now, now, now),
        )
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id="msg_accepted_delivery",
            session_id="ses_accepted",
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=scope_id,
                session_id="ses_accepted",
                platform="telegram",
                author="user",
                source="user",
                message_type="user",
                text="accepted",
                native_message_id="1",
            ),
            dispatch_text="accepted",
            dedupe_key="telegram:1",
            now=now,
        )
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id="turn_accepted_delivery",
            session_id="ses_accepted",
            backend="codex",
            deliveries=[delivery],
            dispatch_text="accepted",
        )
        turn = message_deliveries.bind_native_start(
            conn,
            "turn_accepted_delivery",
            expected_version=int(claimed["turn"]["version"]),
            runtime_key="runtime",
            runtime_turn_id="runtime-turn",
            native_turn_id="native-turn",
        )
        assert turn is not None
        assert message_deliveries.materialize_start_acceptance(
            conn,
            turn_id="turn_accepted_delivery",
            evidence={"kind": "test"},
        )

    command.upgrade(migrations.alembic_config(db_path), "head")

    with engine.connect() as conn:
        accepted = conn.exec_driver_sql(
            "select message_id, dedupe_key from message_deliveries "
            "where id='msg_accepted_delivery'"
        ).one()
        message = conn.exec_driver_sql(
            "select scope_id, native_message_id from messages "
            "where id='msg_accepted_delivery'"
        ).one()
        foreign_key_errors = conn.exec_driver_sql("pragma foreign_key_check").all()
    assert accepted == (
        "msg_accepted_delivery",
        message_deliveries.native_dedupe_key(
            "telegram",
            "1",
            scope_id=scope_id,
        ),
    )
    assert message == (scope_id, "1")
    assert foreign_key_errors == []

    command.downgrade(migrations.alembic_config(db_path), "20260801_0044")
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "select message_id, dedupe_key from message_deliveries "
            "where id='msg_accepted_delivery'"
        ).one() == ("msg_accepted_delivery", "telegram:1")
        assert conn.exec_driver_sql("pragma foreign_key_check").all() == []


def test_session_delivery_fsm_upgrade_and_downgrade_preserve_existing_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260729_0042")
    now = "2026-07-31T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_id, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, session_anchor, workdir, native_session_id,
                title, status, visibility, pinned, agent_status, created_at,
                updated_at, last_active_at, metadata_json
            ) values (
                'ses_fsm', null, null, 'codex', 'codex', 'codex', null, null,
                'ses_fsm', '/tmp', '', null, 'active', 'foreground', 0, 'idle',
                ?, ?, ?, '{}'
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, author_id,
                author_name, source, native_message_id, parent_native_message_id,
                content_text, content_json, metadata_json, created_at, updated_at,
                delivered_at, read_at
            ) values (
                'msg_fsm', null, 'ses_fsm', 'avibe', 'user', 'queued', null,
                null, 'user', null, null, 'hello', '{"text":"hello"}', '{}',
                ?, ?, null, null
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, author_id,
                author_name, source, native_message_id, parent_native_message_id,
                content_text, content_json, metadata_json, created_at, updated_at,
                delivered_at, read_at
            ) values (
                'msg_fsm_agent_run', null, 'ses_fsm', 'avibe', 'harness',
                'queued', null, null, 'harness', 'agent_run:run-legacy',
                null, 'run prompt', '{"text":"run prompt"}', '{}', ?, ?, null, null
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, author_id,
                author_name, source, native_message_id, parent_native_message_id,
                content_text, content_json, metadata_json, created_at, updated_at,
                delivered_at, read_at
            ) values (
                'msg_fsm_dedupe', null, 'ses_fsm', 'avibe', 'harness',
                'harness_dedupe', null, null, 'harness', 'watch:legacy:run-1',
                null, '', '{}', '{}', ?, ?, null, null
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, author_id,
                author_name, source, native_message_id, parent_native_message_id,
                content_text, content_json, metadata_json, created_at, updated_at,
                delivered_at, read_at
            ) values (
                'msg_fsm_draft', null, 'ses_fsm', 'avibe', 'user', 'draft', null,
                null, 'user', null, null, 'unfinished thought',
                '{"text":"unfinished thought"}', '{}', ?, ?, null, null
            )
            """,
            (now, now),
        )
        conn.commit()

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        turn_columns = {
            row[1] for row in conn.execute("pragma table_info(session_turns)")
        }
        delivery_columns = {
            row[1] for row in conn.execute("pragma table_info(message_deliveries)")
        }
        existing = conn.execute(
            "select content_text, type from messages where id = 'msg_fsm'"
        ).fetchone()
        delivery = conn.execute(
            "select state, dispatch_text, snapshot_json from message_deliveries where id = 'msg_fsm'"
        ).fetchone()
        migrated_dedupe = conn.execute(
            "select state, dedupe_key, snapshot_json from message_deliveries "
            "where id = 'msg_fsm_dedupe'"
        ).fetchone()
        migrated_agent_run = conn.execute(
            "select state, dedupe_key, snapshot_json from message_deliveries "
            "where id = 'msg_fsm_agent_run'"
        ).fetchone()
        session_columns = {
            row[1] for row in conn.execute("pragma table_info(agent_sessions)")
        }
        draft_state = conn.execute(
            "select composer_draft_text, composer_draft_updated_at "
            "from agent_sessions where id = 'ses_fsm'"
        ).fetchone()
        message_session_fk = next(
            row
            for row in conn.execute("pragma foreign_key_list(messages)")
            if row[2] == "agent_sessions" and row[3] == "session_id"
        )
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert {"session_turns", "message_deliveries"}.issubset(tables)
    assert {
        "initial_delivery_id",
        "runtime_turn_id",
        "native_turn_id",
        "terminal_outcome",
        "version",
    }.issubset(turn_columns)
    assert {
        "message_id",
        "dispatch_text",
        "priority",
        "current_target_turn_id",
        "current_attempt_id",
        "current_expected_native_turn_id",
        "current_receipt_outcome",
        "delivery_history_json",
        "version",
    }.issubset(delivery_columns)
    assert existing is None
    assert delivery[0:2] == ("queued", "hello")
    assert json.loads(delivery[2])["content_text"] == "hello"
    assert migrated_dedupe[0:2] == ("retired", "avibe:watch:legacy:run-1")
    assert json.loads(migrated_dedupe[2])["type"] == "harness"
    assert migrated_agent_run[0:2] == ("queued", "avibe:agent_run:run-legacy")
    assert json.loads(migrated_agent_run[2])["type"] == "harness"
    assert not {"queue_hold_state", "queue_hold_version", "queue_held_at"} & session_columns
    assert draft_state == ("unfinished thought", now)
    assert message_session_fk[6] == "NO ACTION"
    assert version == (HEAD_REVISION,)

    from storage import message_deliveries

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            assert message_deliveries.queued_session_ids_without_live_turns(conn) == [
                "ses_fsm"
            ]
    finally:
        engine.dispose()

    command.downgrade(migrations.alembic_config(db_path), "20260729_0042")
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        existing = conn.execute(
            "select content_text, type, session_id from messages where id = 'msg_fsm'"
        ).fetchone()
        restored_draft = conn.execute(
            "select content_text, type from messages "
            "where session_id = 'ses_fsm' and type = 'draft'"
        ).fetchone()
        restored_dedupe = conn.execute(
            "select type, native_message_id from messages where id = 'msg_fsm_dedupe'"
        ).fetchone()
        restored_agent_run = conn.execute(
            "select type, native_message_id from messages "
            "where id = 'msg_fsm_agent_run'"
        ).fetchone()
        message_session_fk = next(
            row
            for row in conn.execute("pragma foreign_key_list(messages)")
            if row[2] == "agent_sessions" and row[3] == "session_id"
        )
        versions = {
            row[0]
            for row in conn.execute("select version_num from alembic_version")
        }
    assert "session_turns" not in tables
    assert "message_deliveries" not in tables
    assert existing == ("hello", "queued", "ses_fsm")
    assert restored_draft == ("unfinished thought", "draft")
    assert restored_dedupe == ("harness_dedupe", "watch:legacy:run-1")
    assert restored_agent_run == ("queued", "agent_run:run-legacy")
    assert message_session_fk[6] == "SET NULL"
    assert versions == {"20260725_0038", "20260729_0042"}


def test_session_delivery_migration_uses_live_dedupe_for_harness_provenance(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260729_0042")
    now = "2026-07-31T00:00:00Z"
    cases = [
        (
            "human",
            "user",
            "user",
            None,
            "human:1",
            "user",
            "legacy:avibe:human:1",
        ),
        (
            "task",
            "harness",
            "harness",
            None,
            "task:1",
            "harness",
            "avibe:task:1",
        ),
        (
            "watch",
            "harness",
            "harness",
            None,
            "watch:1",
            "harness",
            "avibe:watch:1",
        ),
        (
            "webhook",
            "harness",
            "harness",
            None,
            "webhook:1",
            "harness",
            "avibe:webhook:1",
        ),
        (
            "show",
            "harness",
            "harness",
            "show_annotation",
            "show:1",
            "annotation",
            "avibe:show:1",
        ),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_id, agent_name, agent_backend, agent_variant,
                model, reasoning_effort, session_anchor, workdir, native_session_id,
                title, status, visibility, pinned, agent_status, created_at,
                updated_at, last_active_at, metadata_json
            ) values (
                'ses_dedupe_classifier', null, null, 'codex', 'codex', 'codex',
                null, null, 'ses_dedupe_classifier', '/tmp', '', null, 'active',
                'foreground', 0, 'idle', ?, ?, ?, '{}'
            )
            """,
            (now, now, now),
        )
        for name, author, source, author_name, native_id, _type, _dedupe in cases:
            conn.execute(
                """
                insert into messages (
                    id, scope_id, session_id, platform, author, type, author_id,
                    author_name, source, native_message_id, parent_native_message_id,
                    content_text, content_json, metadata_json, created_at, updated_at,
                    delivered_at, read_at
                ) values (
                    ?, null, 'ses_dedupe_classifier', 'avibe', ?, 'queued', null,
                    ?, ?, ?, null, ?, json_object('text', ?), '{}', ?, ?, null, null
                )
                """,
                (
                    f"msg_{name}",
                    author,
                    author_name,
                    source,
                    native_id,
                    name,
                    name,
                    now,
                    now,
                ),
            )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        migrated = conn.execute(
            "select id, dedupe_key, snapshot_json from message_deliveries "
            "where session_id = 'ses_dedupe_classifier' order by id"
        ).fetchall()
    by_id = {
        row_id: (dedupe_key, json.loads(snapshot_json)["type"])
        for row_id, dedupe_key, snapshot_json in migrated
    }
    assert by_id == {
        f"msg_{name}": (expected_dedupe, expected_type)
        for (
            name,
            _author,
            _source,
            _author_name,
            _native_id,
            expected_type,
            expected_dedupe,
        ) in cases
    }


def test_session_delivery_migration_binds_each_live_scheduled_run(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260729_0042")
    now = "2026-07-31T00:00:00Z"
    cases = (
        ("task", "scheduled", "queued"),
        ("watch", "watch", "pending"),
        ("webhook", "hook", "processing"),
    )
    with sqlite3.connect(db_path) as conn:
        for name, trigger_kind, run_status in cases:
            session_id = f"ses_{name}_migration"
            run_id = f"run_{name}_migration"
            message_id = f"msg_{name}_migration"
            native_id = f"{name}:migration"
            conn.execute(
                """
                insert into agent_sessions (
                    id, scope_id, agent_id, agent_name, agent_backend, agent_variant,
                    model, reasoning_effort, session_anchor, workdir, native_session_id,
                    title, status, visibility, pinned, agent_status, created_at,
                    updated_at, last_active_at, metadata_json
                ) values (
                    ?, null, null, 'codex', 'codex', 'codex', null, null,
                    ?, '/tmp', '', null, 'active', 'foreground', 0, 'idle',
                    ?, ?, ?, '{}'
                )
                """,
                (session_id, session_id, now, now, now),
            )
            conn.execute(
                """
                insert into agent_runs (
                    id, run_type, status, session_id, cancel_requested,
                    created_at, updated_at, metadata_json
                ) values (?, ?, ?, ?, 0, ?, ?, '{}')
                """,
                (run_id, name, run_status, session_id, now, now),
            )
            metadata_json = json.dumps(
                {
                    "scheduled_provenance": {
                        "platform_specific": {
                            "task_execution_id": run_id,
                            "task_trigger_kind": trigger_kind,
                        }
                    }
                }
            )
            conn.execute(
                """
                insert into messages (
                    id, scope_id, session_id, platform, author, type, author_id,
                    author_name, source, native_message_id, parent_native_message_id,
                    content_text, content_json, metadata_json, created_at, updated_at,
                    delivered_at, read_at
                ) values (
                    ?, null, ?, 'avibe', 'harness', 'queued', null, ?, 'harness',
                    ?, null, ?, json_object('text', ?), ?, ?, ?, null, null
                )
                """,
                (
                    message_id,
                    session_id,
                    name,
                    native_id,
                    name,
                    name,
                    metadata_json,
                    now,
                    now,
                ),
            )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        migrated = conn.execute(
            """
            select r.id, r.delivery_id, d.state, d.dedupe_key
            from agent_runs r
            join message_deliveries d on d.id = r.delivery_id
            join agent_sessions s on s.id = r.session_id
            where r.id like 'run_%_migration'
            order by r.id
            """
        ).fetchall()
    assert migrated == [
        (
            f"run_{name}_migration",
            f"msg_{name}_migration",
            "queued",
            f"avibe:{name}:migration",
        )
        for name, _trigger_kind, _run_status in sorted(cases)
    ]


def test_session_delivery_migration_dedupes_and_avoids_legacy_event_id_collisions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260729_0042")
    now = "2026-07-31T00:00:00Z"
    tool_message_id = "msg_tool_collision"
    silent_message_id = "msg_silent_existing"
    tool_event_id = (
        "evt_legacy_"
        + hashlib.sha256(f"tool_call:{tool_message_id}".encode()).hexdigest()[:24]
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, is_private, supports_threads,
                metadata_json, first_seen_at, last_seen_at, updated_at
            ) values ('scope_fsm_trace', 'avibe', 'project', 'trace', 0, 0,
                '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_name, agent_backend, agent_variant,
                session_anchor, workdir, native_session_id, status, visibility,
                pinned, agent_status, metadata_json, created_at, updated_at,
                last_active_at
            ) values ('ses_fsm_trace', 'scope_fsm_trace', 'codex', 'codex',
                'codex', 'trace', '/tmp', '', 'active', 'foreground', 0,
                'idle', '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.executemany(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, content_text,
                content_json, metadata_json, created_at, updated_at
            ) values (?, 'scope_fsm_trace', 'ses_fsm_trace', 'avibe', 'agent',
                ?, ?, '{}', '{}', ?, ?)
            """,
            (
                (tool_message_id, "tool_call", "tool trace", now, now),
                (silent_message_id, "silent", "silent trace", now, now),
            ),
        )
        conn.executemany(
            """
            insert into agent_events (
                id, scope_id, session_id, platform, event_type, visibility,
                content_json, metadata_json, created_at, updated_at
            ) values (?, 'scope_fsm_trace', 'ses_fsm_trace', 'avibe', ?,
                'trace', '{}', ?, ?, ?)
            """,
            (
                (tool_event_id, "unrelated", "{}", now, now),
                (
                    "evt_existing_silent",
                    "silent_terminal",
                    json.dumps({"legacy_message_id": silent_message_id}),
                    now,
                    now,
                ),
            ),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, content_text,
                content_json, metadata_json, created_at, updated_at
            ) values ('msg_accepted_ref', 'scope_fsm_trace', 'ses_fsm_trace',
                'avibe', 'agent', 'result', 'accepted', '{}', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, message_id, created_at
            ) values ('show_accepted_ref', 'ses_fsm_trace', 'annotation',
                'agent', 'session', '{}', '{}', 'msg_accepted_ref', ?)
            """,
            (now,),
        )
        conn.execute(
            """
            insert into media_objects (
                token, scope_id, session_id, message_id, kind, source,
                local_path, created_at
            ) values ('media_accepted_ref', 'scope_fsm_trace', 'ses_fsm_trace',
                'msg_accepted_ref', 'file', 'agent', '/tmp/accepted.txt', ?)
            """,
            (now,),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        pseudo_count = conn.execute(
            "select count(*) from messages where id in (?, ?)",
            (tool_message_id, silent_message_id),
        ).fetchone()[0]
        tool_events = conn.execute(
            "select id from agent_events where event_type = 'tool_call' "
            "and json_extract(metadata_json, '$.legacy_message_id') = ?",
            (tool_message_id,),
        ).fetchall()
        silent_events = conn.execute(
            "select id from agent_events where event_type = 'silent_terminal' "
            "and json_extract(metadata_json, '$.legacy_message_id') = ?",
            (silent_message_id,),
        ).fetchall()
        collision = conn.execute(
            "select event_type from agent_events where id = ?",
            (tool_event_id,),
        ).fetchone()
        accepted_refs = (
            conn.execute(
                "select message_id from show_session_events "
                "where id = 'show_accepted_ref'"
            ).fetchone(),
            conn.execute(
                "select message_id from media_objects "
                "where token = 'media_accepted_ref'"
            ).fetchone(),
        )
    assert pseudo_count == 0
    assert tool_events == [(f"{tool_event_id}_1",)]
    assert silent_events == [("evt_existing_silent",)]
    assert collision == ("unrelated",)
    assert accepted_refs == (("msg_accepted_ref",), ("msg_accepted_ref",))

    from storage import agent_activity_service

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            groups = agent_activity_service.list_turn_groups(
                conn,
                session_id="ses_fsm_trace",
            )["groups"]
    finally:
        engine.dispose()
    assert [group["status"] for group in groups] == ["done"]
    assert groups[0]["steps"] == 1

    command.downgrade(migrations.alembic_config(db_path), "20260729_0042")
    with sqlite3.connect(db_path) as conn:
        restored_trace = conn.execute(
            "select id, type, content_text from messages "
            "where id in (?, ?) order by id",
            (tool_message_id, silent_message_id),
        ).fetchall()
        remaining_events = conn.execute(
            "select id, metadata_json from agent_events order by id"
        ).fetchall()
        downgraded_refs = (
            conn.execute(
                "select message_id from show_session_events "
                "where id = 'show_accepted_ref'"
            ).fetchone(),
            conn.execute(
                "select message_id from media_objects "
                "where token = 'media_accepted_ref'"
            ).fetchone(),
        )
    assert restored_trace == [
        (silent_message_id, "silent", "silent trace"),
        (tool_message_id, "tool_call", "tool trace"),
    ]
    assert {row[0] for row in remaining_events} == {
        tool_event_id,
        "evt_existing_silent",
    }
    metadata_by_id = {row[0]: json.loads(row[1]) for row in remaining_events}
    assert metadata_by_id["evt_existing_silent"] == {
        "legacy_message_id": silent_message_id
    }
    assert downgraded_refs == (("msg_accepted_ref",), ("msg_accepted_ref",))


def test_session_delivery_migration_refuses_preexisting_operational_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260731_0043")
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table message_deliveries (id text primary key)")
        conn.execute("create table session_turns (id text primary key)")
        conn.commit()

    with pytest.raises(RuntimeError, match="interrupted 0044 migration"):
        run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone() == (
            "20260731_0043",
        )


def test_session_delivery_migration_completes_precreated_operational_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260731_0043")
    now = "2026-07-31T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_name, agent_backend, agent_variant,
                session_anchor, workdir, native_session_id, status, visibility,
                pinned, agent_status, metadata_json, created_at, updated_at,
                last_active_at
            ) values ('ses_interrupted', null, 'codex', 'codex', 'codex',
                'interrupted', '/tmp', '', 'active', 'foreground', 0,
                'idle', '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, source,
                content_text, content_json, metadata_json, created_at, updated_at
            ) values (
                'msg_interrupted', null, 'ses_interrupted', 'avibe', 'user',
                'queued', 'user', 'resume me', '{"text":"resume me"}', '{}', ?, ?
            )
            """,
            (now, now),
        )
        conn.commit()

    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(
            engine,
            tables=[
                metadata.tables["message_deliveries"],
                metadata.tables["session_turns"],
            ],
        )
    finally:
        engine.dispose()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select 1 from messages where id = 'msg_interrupted'"
        ).fetchone() is None
        assert conn.execute(
            "select state, session_id from message_deliveries "
            "where id = 'msg_interrupted'"
        ).fetchone() == ("queued", "ses_interrupted")
        assert conn.execute(
            "select 1 from pragma_table_info('agent_runs') where name = 'delivery_id'"
        ).fetchone() == (1,)
        assert conn.execute(
            "select 1 from pragma_index_list('agent_runs') "
            "where name = 'uq_agent_runs_delivery'"
        ).fetchone() == (1,)


def test_session_delivery_migration_resolves_legacy_pending_by_its_real_owner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260729_0042")
    now = "2026-07-31T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_name, agent_backend, agent_variant,
                session_anchor, workdir, native_session_id, status, visibility,
                pinned, agent_status, metadata_json, created_at, updated_at,
                last_active_at
            ) values ('ses_pending', null, 'codex', 'codex', 'codex',
                'pending', '/tmp', '', 'active', 'foreground', 0,
                'idle', '{}', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.executemany(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, author_name,
                source, content_text, content_json, metadata_json, created_at,
                updated_at
            ) values (?, null, 'ses_pending', 'avibe', ?, 'pending', ?, ?, ?,
                json_object('text', ?), '{}', ?, ?)
            """,
            (
                ("msg_pending_human", "user", None, "user", "human", "human", now, now),
                (
                    "msg_pending_harness",
                    "harness",
                    "watch",
                    "harness",
                    "scheduled",
                    "scheduled",
                    now,
                    now,
                ),
                (
                    "msg_pending_annotation",
                    "harness",
                    "show_annotation",
                    "harness",
                    "annotation",
                    "annotation",
                    now,
                    now,
                ),
            ),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        remaining_messages = conn.execute(
            "select id from messages where id in "
            "('msg_pending_human', 'msg_pending_harness', 'msg_pending_annotation')"
        ).fetchall()
        deliveries = conn.execute(
            "select id, state, current_attempt_kind, current_receipt_outcome, "
            "snapshot_json from message_deliveries where id in "
            "('msg_pending_human', 'msg_pending_harness', 'msg_pending_annotation') "
            "order by id"
        ).fetchall()
    assert remaining_messages == []
    assert [(row[0], row[1], row[2], row[3]) for row in deliveries] == [
        ("msg_pending_annotation", "retired", None, None),
        ("msg_pending_harness", "retired", None, None),
        ("msg_pending_human", "retired", None, None),
    ]
    snapshots = {row[0]: json.loads(row[4]) for row in deliveries}
    assert snapshots["msg_pending_annotation"]["type"] == "annotation"
    assert snapshots["msg_pending_annotation"]["author"] == "harness"
    assert snapshots["msg_pending_harness"]["type"] == "harness"
    assert snapshots["msg_pending_harness"]["author"] == "harness"
    assert snapshots["msg_pending_harness"]["author_name"] == "watch"
    assert snapshots["msg_pending_harness"]["source"] == "harness"
    assert snapshots["msg_pending_human"]["type"] == "user"
    assert snapshots["msg_pending_human"]["author"] == "user"
    assert snapshots["msg_pending_human"]["source"] == "user"
    from storage import message_deliveries

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            assert message_deliveries.recoverable_reservations(conn, "ses_pending") == []
            assert message_deliveries.ordering_head(conn, "ses_pending") is None
    finally:
        engine.dispose()

    command.downgrade(migrations.alembic_config(db_path), "20260729_0042")
    with sqlite3.connect(db_path) as conn:
        restored = conn.execute(
            "select id, type, author from messages where id in "
            "('msg_pending_human', 'msg_pending_harness', 'msg_pending_annotation') "
            "order by id"
        ).fetchall()
    assert restored == [
        ("msg_pending_annotation", "pending", "harness"),
        ("msg_pending_harness", "pending", "harness"),
        ("msg_pending_human", "pending", "user"),
    ]


def test_upgrade_keeps_historical_conflated_callback_rows_sent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260726_0037")
    now = "2026-07-30T00:00:00Z"

    with sqlite3.connect(db_path) as conn:
        def insert_run(
            run_id: str,
            *,
            source_kind: str,
            parent_run_id: str | None = None,
            session_id: str | None = None,
            callback_session_id: str | None = None,
            callback_status: str | None = None,
            callback_run_id: str | None = None,
        ) -> None:
            conn.execute(
                """
                insert into agent_runs (
                    id, run_type, status, source_kind, parent_run_id, session_id,
                    callback_session_id, callback_status, callback_error,
                    callback_run_id, callback_completed_at, cancel_requested,
                    created_at, completed_at, updated_at, metadata_json
                ) values (
                    ?, 'agent_run', 'succeeded', ?, ?, ?, ?, ?, 'legacy error',
                    ?, ?, 0, ?, ?, ?, '{}'
                )
                """,
                (
                    run_id,
                    source_kind,
                    parent_run_id,
                    session_id,
                    callback_session_id,
                    callback_status,
                    callback_run_id,
                    now if callback_status else None,
                    now,
                    now if callback_status else None,
                    now,
                ),
            )

        insert_run(
            "historical_parent",
            source_kind="agent",
            callback_session_id="ses_caller",
            callback_status="sent",
            callback_run_id="directed_child",
        )
        insert_run(
            "directed_child",
            source_kind="agent",
            parent_run_id="historical_parent",
            session_id="ses_caller",
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        parent = conn.execute(
            """
            select callback_status, callback_error, callback_run_id,
                   callback_completed_at
            from agent_runs
            where id = 'historical_parent'
            """
        ).fetchone()
        child = conn.execute(
            """
            select source_kind, parent_run_id, session_id
            from agent_runs
            where id = 'directed_child'
            """
        ).fetchone()
        callback_count = conn.execute(
            "select count(*) from agent_runs where source_kind = 'callback'"
        ).fetchone()
        version = conn.execute(
            "select version_num from alembic_version"
        ).fetchone()

    assert parent == (
        "sent",
        "legacy error",
        "directed_child",
        now,
    )
    assert child == ("agent", "historical_parent", "ses_caller")
    assert callback_count == (0,)
    assert version == (HEAD_REVISION,)


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
        assert "ix_messages_session_transcript_id" in message_indexes
        assert "ix_messages_session_type_created_id" in message_indexes
        assert "ix_messages_platform_session_created_id" in message_indexes
        assert "ix_messages_unread_session" in message_indexes
        assert "ix_messages_mark_read" in message_indexes
        assert "ix_messages_inbox_activity" in message_indexes
        assert "ix_messages_inbox_agent_reply" in message_indexes
        assert "ix_messages_inbox_user_send" in message_indexes
        assert "session_id is not null" in _index_sql(
            conn, "ix_messages_inbox_activity"
        )
        assert "harness_dedupe" not in _index_sql(
            conn, "ix_messages_inbox_activity"
        )
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
        assert "ix_agent_sessions_scope_status_pinned_activity" in agent_session_indexes
        agent_session_columns = {
            row[1]: row for row in conn.execute("pragma table_info(agent_sessions)")
        }
        assert agent_session_columns["pinned"][3] == 1
        assert str(agent_session_columns["pinned"][4]).strip("'") == "0"
        media_columns = {
            row[1] for row in conn.execute("pragma table_info(media_objects)")
        }
        media_scope_not_null = {
            row[1]: row[3] for row in conn.execute("pragma table_info(media_objects)")
        }["scope_id"]
        assert "mtime_ns" in media_columns  # 20260603_0014: dedup fingerprint
        assert "width_px" in media_columns  # 20260604_0015: zero-shift image box
        assert "height_px" in media_columns
        assert media_scope_not_null == 0  # standalone sessions can own uploads
        show_event_columns = {
            row[1]: row for row in conn.execute("pragma table_info(show_session_events)")
        }
        assert "dispatch_state" not in show_event_columns
        background_columns = {
            row[1]
            for row in conn.execute(
                "pragma table_info(run_definitions)",
            )
        }
        assert "deleted_at" in background_columns
        version = conn.execute("select version_num from alembic_version").fetchone()
        assert version == (HEAD_REVISION,)


def test_show_dispatch_state_removal_migration_preserves_existing_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260726_0036")
    now = "2026-07-26T00:00:00Z"

    with sqlite3.connect(db_path) as conn:
        for message_id, author, message_type in (
            ("msg_upgrade_accepted", "user", "pending"),
            ("msg_upgrade_retryable", "user", "pending"),
            ("msg_upgrade_visible", "user", "user"),
            ("msg_upgrade_observed", "user", "user"),
            ("msg_upgrade_chat", "user", "pending"),
        ):
            conn.execute(
                """
                insert into messages (
                    id, platform, author, type, content_json,
                    metadata_json, created_at, updated_at
                ) values (?, 'avibe', ?, ?, '{}', '{}', ?, ?)
                """,
                (message_id, author, message_type, now, now),
            )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, transcript_text, message_id, dispatch_state,
                created_at
            ) values (
                'show_evt_legacy', 'ses_legacy', 'human.annotation.created',
                'human', 'default', '{}', '{}', 'Legacy annotation', null,
                '{"state":"in_flight","owner":"1:old"}', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, transcript_text, message_id, dispatch_state,
                created_at
            ) values (
                'show_evt_upgrade_accepted', 'ses_upgrade',
                'human.annotation.created', 'human', 'default', '{}',
                '{"dispatch":true}',
                'Accepted annotation', 'msg_upgrade_accepted',
                '{"state":"accepted"}', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, transcript_text, message_id, dispatch_state,
                created_at
            ) values (
                'show_evt_upgrade_retryable', 'ses_upgrade',
                'human.intent.submitted', 'human', 'default', '{}',
                '{"dispatch":true}',
                'Retryable intent', 'msg_upgrade_retryable',
                '{"state":"failed"}', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, transcript_text, message_id, dispatch_state,
                created_at
            ) values (
                'show_evt_upgrade_visible', 'ses_upgrade',
                'human.annotation.created', 'human', 'default', '{}',
                '{"dispatch":true}',
                'Visible annotation', 'msg_upgrade_visible',
                '{"state":"failed"}', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            insert into show_session_events (
                id, session_id, event_type, actor, scope, anchor_json,
                payload_json, transcript_text, message_id, dispatch_state,
                created_at
            ) values (
                'show_evt_upgrade_observed', 'ses_upgrade',
                'human.annotation.created', 'human', 'default', '{}', '{}',
                'Observed annotation', 'msg_upgrade_observed',
                '{"state":"accepted"}', ?
            )
            """,
            (now,),
        )
        conn.commit()

    real_connect = sqlite3.connect

    def legacy_connect(*args, **kwargs):
        kwargs["factory"] = _Pre335Connection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", legacy_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", legacy_connect)
    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("pragma table_info(show_session_events)")
        }
        legacy = conn.execute(
            "select id, transcript_text from show_session_events "
            "where id = 'show_evt_legacy'"
        ).fetchone()
        upgraded_messages = {
            row[0]: row[1:]
            for row in conn.execute(
                "select id, author, type, source, author_name, author_id "
                "from messages "
                "where id in ("
                "'msg_upgrade_accepted', "
                "'msg_upgrade_retryable', "
                "'msg_upgrade_visible', "
                "'msg_upgrade_observed', "
                "'msg_upgrade_chat'"
                ")"
            ).fetchall()
        }
        retryable_trace = conn.execute(
            "select event_type, json_extract(metadata_json, '$.legacy_message_id') "
            "from agent_events where json_extract(metadata_json, '$.legacy_message_id') "
            "= 'msg_upgrade_retryable'"
        ).fetchone()
        chat_trace = conn.execute(
            "select event_type, json_extract(metadata_json, '$.legacy_message_id') "
            "from agent_events where json_extract(metadata_json, '$.legacy_message_id') "
            "= 'msg_upgrade_chat'"
        ).fetchone()
        for message_id, message_type, event_id in (
            ("msg_accepted_show", "harness", "show_evt_accepted"),
        ):
            conn.execute(
                """
                insert into messages (
                    id, platform, author, type, source, author_name, author_id,
                    content_json,
                    metadata_json, created_at, updated_at
                ) values (
                    ?, 'avibe', 'harness', ?, 'harness', 'show_annotation',
                    ?, '{}', '{}', ?, ?
                )
                """,
                (message_id, message_type, event_id, now, now),
            )
        for event_id, message_id in (("show_evt_accepted", "msg_accepted_show"),):
            conn.execute(
                """
                insert into show_session_events (
                    id, session_id, event_type, actor, scope, anchor_json,
                    payload_json, transcript_text, message_id, created_at
                ) values (
                    ?, 'ses_downgrade', 'human.annotation.created',
                    'human', 'default', '{}', '{"dispatch":true}',
                    'Downgrade annotation', ?, ?
                )
                """,
                (event_id, message_id, now),
            )
        conn.commit()

    assert "dispatch_state" not in columns
    assert legacy == ("show_evt_legacy", "Legacy annotation")
    assert upgraded_messages["msg_upgrade_accepted"] == (
        "harness",
        "harness",
        "harness",
        "show_annotation",
        "show_evt_upgrade_accepted",
    )
    assert "msg_upgrade_retryable" not in upgraded_messages
    assert retryable_trace == ("tool_call", "msg_upgrade_retryable")
    assert upgraded_messages["msg_upgrade_visible"] == (
        "harness",
        "harness",
        "harness",
        "show_annotation",
        "show_evt_upgrade_visible",
    )
    assert upgraded_messages["msg_upgrade_observed"] == (
        "user",
        "user",
        None,
        None,
        None,
    )
    assert "msg_upgrade_chat" not in upgraded_messages
    assert chat_trace == ("tool_call", "msg_upgrade_chat")

    command.downgrade(migrations.alembic_config(db_path), "20260726_0036")

    with sqlite3.connect(db_path) as conn:
        downgraded = dict(
            conn.execute(
                "select id, dispatch_state from show_session_events "
                "where id in ('show_evt_legacy', 'show_evt_accepted')"
            ).fetchall()
        )
        downgraded_messages = {
            row[0]: row[1:]
            for row in conn.execute(
                "select id, author, type, source, author_name, author_id "
                "from messages "
                "where id = 'msg_accepted_show'"
            ).fetchall()
        }

    assert json.loads(downgraded["show_evt_legacy"]) == {"state": "accepted"}
    assert json.loads(downgraded["show_evt_accepted"]) == {"state": "accepted"}
    assert downgraded_messages["msg_accepted_show"] == (
        "user",
        "user",
        None,
        None,
        None,
    )


def test_retirement_marker_migration_is_forward_only(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260726_0035")

    with sqlite3.connect(db_path) as conn:
        assert "retired_at" not in {
            row[1] for row in conn.execute("pragma table_info(run_definitions)")
        }
        conn.execute(
            """
            insert into run_definitions (
                id, definition_type, mode, enabled, last_finished_at,
                created_at, updated_at, metadata_json
            ) values (
                'legacy-paused-watch', 'watch', 'forever', 0,
                '2026-07-26T00:00:00+00:00',
                '2026-07-26T00:00:00+00:00',
                '2026-07-26T00:00:00+00:00', '{}'
            )
            """
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("pragma table_info(run_definitions)")
        }
        row = conn.execute(
            "select last_finished_at, retired_at from run_definitions where id = ?",
            ("legacy-paused-watch",),
        ).fetchone()
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert "retired_at" in columns
    assert row == ("2026-07-26T00:00:00+00:00", None)
    assert version == (HEAD_REVISION,)


def test_retirement_reason_migration_preserves_legacy_unknowns(tmp_path: Path) -> None:
    """0053 adds no inferred reason from clocks, edits, or run history."""

    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260811_0052")
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            insert into run_definitions (
                id, definition_type, schedule_type, run_at, enabled,
                last_run_at, retired_at, created_at, updated_at, metadata_json
            ) values (?, 'scheduled', 'at', ?, 0, ?, ?, ?, ?, '{}')
            """,
            (
                (
                    "legacy-ran",
                    "2026-07-26T09:00:00+00:00",
                    "2026-07-26T09:01:00+00:00",
                    "2026-07-26T09:01:00+00:00",
                    "2026-07-25T00:00:00+00:00",
                    "2026-07-31T00:00:00+00:00",
                ),
                (
                    "legacy-missed",
                    "2026-07-26T10:00:00+00:00",
                    None,
                    "2026-07-26T10:00:01+00:00",
                    "2026-07-25T00:00:00+00:00",
                    "2026-07-31T00:00:00+00:00",
                ),
            ),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select id, retirement_reason from run_definitions order by id"
        ).fetchall()
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert rows == [("legacy-missed", None), ("legacy-ran", None)]
    assert version == (HEAD_REVISION,)


def test_orphaned_owner_task_migration_marks_unbound_definitions(tmp_path: Path) -> None:
    """0052 blocks every resumable orphan and stops the ones still enabled."""
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260811_0051")

    with sqlite3.connect(db_path) as conn:
        _insert_scope(conn, "sc1")
        for session_id, anchor in (
            ("ses-live-owner", "live-owner"),
            ("ses-target", "target"),
            ("ses-archived-owner", "archived-owner"),
        ):
            _insert_agent_session(
                conn,
                row_id=session_id,
                scope_id="sc1",
                anchor=anchor,
                workdir="/tmp",
                backend="codex",
                native=f"native-{session_id}",
                last_active="2026-08-11T00:00:00+00:00",
            )
        conn.execute(
            "update agent_sessions set status = 'archived' where id = 'ses-archived-owner'"
        )

        def insert_task(
            task_id: str,
            owner_session_id: str | None,
            *,
            session_id: str | None = None,
            session_policy: str | None = "create_per_run",
            enabled: int = 1,
            last_error: str | None = None,
        ) -> None:
            metadata = (
                json.dumps({"created_by": {"caller": {"session_id": owner_session_id}}})
                if owner_session_id is not None
                else "{}"
            )
            conn.execute(
                """
                insert into run_definitions (
                    id, definition_type, name, session_policy, session_id, enabled,
                    last_error, created_at, updated_at, metadata_json
                ) values (?, 'scheduled', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_id,
                    session_policy,
                    session_id,
                    enabled,
                    last_error,
                    "2026-08-11T00:00:00+00:00",
                    "2026-08-11T00:00:00+00:00",
                    metadata,
                ),
            )

        insert_task("missing-owner", "ses-removed-owner")
        insert_task("pure-command", "ses-removed-owner", session_policy=None)
        insert_task("blank-target", "ses-removed-owner", session_id="   ")
        insert_task("whitespace-target", "ses-removed-owner", session_id="\t\n\r")
        insert_task("archived-owner", "ses-archived-owner")
        insert_task("live-owner", "ses-live-owner")
        insert_task("target-fallback", "ses-removed-owner", session_id="ses-target")
        insert_task("legacy-unowned", None)
        insert_task(
            "already-paused",
            "ses-removed-owner",
            enabled=0,
            last_error="manual pause",
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "select id, enabled, session_id, last_error, metadata_json from run_definitions"
            )
        }

    def owner_marker(task_id: str) -> dict | None:
        metadata = json.loads(rows[task_id][3])
        return metadata.get("orphaned_task_owner")

    expected_paused = {
        "missing-owner": (None, "ses-removed-owner"),
        "pure-command": (None, "ses-removed-owner"),
        "blank-target": ("   ", "ses-removed-owner"),
        "whitespace-target": ("\t\n\r", "ses-removed-owner"),
        "archived-owner": (None, "ses-archived-owner"),
    }
    for task_id, (session_id, owner_session_id) in expected_paused.items():
        assert rows[task_id][:3] == (0, session_id, None)
        assert owner_marker(task_id) == {
            "reason_code": "task_owner_session_unavailable",
            "owner_session_id": owner_session_id,
        }

    assert rows["live-owner"][:3] == (1, None, None)
    assert rows["target-fallback"][:3] == (1, "ses-target", None)
    assert rows["legacy-unowned"][:3] == (1, None, None)
    assert rows["already-paused"][:3] == (0, None, "manual pause")
    assert owner_marker("already-paused") == {
        "reason_code": "task_owner_session_unavailable",
        "owner_session_id": "ses-removed-owner",
    }
    for task_id in ("live-owner", "target-fallback", "legacy-unowned"):
        assert owner_marker(task_id) is None


def test_show_annotation_migration_changes_only_the_user_send_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260726_0037")
    now = "2026-07-27T00:00:00.000000Z"
    legacy_payloads = [
        ("agent", "assistant", "assistant.mark.created")
        for _ in range(5)
    ]
    legacy_payloads.extend(
        [
            ("agent", "assistant", "assistant.mark.updated"),
            ("agent", "assistant", "assistant.mark.resolved"),
        ]
    )
    legacy_payloads.extend(
        ("harness", "harness", "human.annotation.created")
        for _ in range(10)
    )
    legacy_payloads.extend(
        ("user", "user", "human.annotation.created")
        for _ in range(10)
    )
    assert len(legacy_payloads) == 27

    with sqlite3.connect(db_path) as conn:
        for index, (author, message_type, show_event_type) in enumerate(
            legacy_payloads
        ):
            conn.execute(
                """
                insert into messages (
                    id, platform, author, type, content_text, content_json,
                    metadata_json, created_at, updated_at
                ) values (?, 'avibe', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"legacy_message_{index:02d}",
                    author,
                    message_type,
                    f"legacy text {index}",
                    json.dumps({"ordinal": index}, separators=(",", ":")),
                    json.dumps(
                        {
                            "source": "show_page",
                            "show_event_type": show_event_type,
                        },
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                ),
            )
        conn.commit()
        original_rows = conn.execute(
            "select * from messages order by id"
        ).fetchall()
        original_index = _index_sql(conn, "ix_messages_inbox_user_send")

    migration = import_module(
        "storage.alembic.versions.20260727_0038_show_annotation_type"
    )
    assert original_index.endswith(migration.DOWNGRADE_USER_SEND_PREDICATE)

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        upgraded_rows = conn.execute(
            "select * from messages order by id"
        ).fetchall()
        user_send_index = _index_sql(conn, "ix_messages_inbox_user_send")
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert upgraded_rows == original_rows
    assert user_send_index.endswith(
        MESSAGE_PARTIAL_INDEX_PREDICATES["ix_messages_inbox_user_send"]
    )
    assert (
        "(platform, session_id, coalesce(delivered_at, created_at) desc, id desc)"
        in user_send_index
    )
    assert version == (HEAD_REVISION,)

    command.downgrade(migrations.alembic_config(db_path), "20260726_0037")

    with sqlite3.connect(db_path) as conn:
        downgraded_rows = conn.execute(
            "select * from messages order by id"
        ).fetchall()
        user_send_index = _index_sql(conn, "ix_messages_inbox_user_send")
        versions = {
            row[0]
            for row in conn.execute("select version_num from alembic_version")
        }

    assert downgraded_rows == original_rows
    assert user_send_index.endswith(migration.DOWNGRADE_USER_SEND_PREDICATE)
    assert "(platform, session_id, created_at desc, id desc)" in user_send_index
    assert versions == {"20260725_0038", "20260726_0037"}


def test_session_pinning_migration_preserves_existing_sessions_as_unpinned(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260723_0033")
    now = "2026-07-24T00:00:00Z"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, native_type, is_private,
                supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'scope_pin', 'avibe', 'project', 'proj_pin', null, 0,
                0, '{}', ?, ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            insert into agent_sessions (
                id, scope_id, agent_backend, agent_variant, session_anchor,
                workdir, native_session_id, title, status, visibility, agent_status,
                metadata_json, created_at, updated_at, last_active_at
            ) values (
                'ses_existing', 'scope_pin', 'codex', 'codex', 'ses_existing',
                '/tmp/project', '', 'Existing session', 'active', 'foreground', 'idle',
                '{}', ?, ?, ?
            )
            """,
            (now, now, now),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        pinned = conn.execute(
            "select pinned from agent_sessions where id = 'ses_existing'"
        ).fetchone()
        indexes = {
            row[1] for row in conn.execute("pragma index_list('agent_sessions')")
        }
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert pinned == (0,)
    assert "ix_agent_sessions_scope_status_pinned_activity" in indexes
    assert version == (HEAD_REVISION,)


def test_session_visibility_migration_reparents_legacy_runs_and_self_anchors(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260721_0031")
    now = "2026-07-23T00:00:00Z"

    with sqlite3.connect(db_path) as conn:
        for scope_id, native_type in (
            ("scope_real", None),
            ("scope_private_a", "private_agent_run"),
            ("scope_private_b", "private_agent_run"),
            ("scope_private_c", "private_agent_run"),
        ):
            conn.execute(
                """
                insert into scopes (
                    id, platform, scope_type, native_id, native_type, is_private,
                    supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
                ) values (?, 'avibe', 'project', ?, ?, 0, 0, '{}', ?, ?, ?)
                """,
                (scope_id, scope_id, native_type, now, now, now),
            )

        def insert_session(session_id: str, scope_id: str, anchor: str, workdir: str) -> None:
            conn.execute(
                """
                insert into agent_sessions (
                    id, scope_id, agent_backend, agent_variant, session_anchor,
                    workdir, native_session_id, status, agent_status, metadata_json,
                    created_at, updated_at, last_active_at
                ) values (?, ?, 'codex', 'codex', ?, ?, '', 'active', 'idle', '{}', ?, ?, ?)
                """,
                (session_id, scope_id, anchor, workdir, now, now, now),
            )

        insert_session("ses_caller", "scope_real", "caller", "/caller")
        insert_session("ses_source", "scope_real", "source", "/source")
        insert_session("ses_legacy_a", "scope_private_a", "same-anchor", "/legacy-a")
        insert_session("ses_legacy_b", "scope_private_b", "same-anchor", "/legacy-b")
        insert_session("ses_legacy_c", "scope_private_c", "unresolved", "/legacy-c")

        conn.execute(
            """
            insert into media_objects (
                token, scope_id, session_id, kind, source, local_path, created_at
            ) values (
                'media_existing', 'scope_real', 'ses_caller', 'file',
                'user_upload', '/tmp/existing.txt', ?
            )
            """,
            (now,),
        )

        conn.execute(
            """
            insert into agent_runs (
                id, run_type, status, source_kind, source_actor, session_id,
                cancel_requested, created_at, updated_at, metadata_json
            ) values (
                'run_source_actor', 'agent_run', 'succeeded', 'agent', 'ses_caller',
                'ses_legacy_a', 0, ?, ?, '{}'
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            insert into agent_runs (
                id, run_type, status, source_kind, source_actor, session_id,
                cancel_requested, created_at, updated_at, metadata_json
            ) values (
                'run_metadata', 'agent_run', 'succeeded', 'agent', 'agent:worker',
                'ses_legacy_b', 0, ?, ?, ?
            )
            """,
            (now, now, json.dumps({"caller_context": {"session_id": "ses_source"}})),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            select id, scope_id, session_anchor, visibility, workdir
            from agent_sessions where id like 'ses_legacy_%' order by id
            """
        ).fetchall()
        pseudo_scope_count = conn.execute(
            "select count(*) from scopes where native_type = 'private_agent_run'"
        ).fetchone()[0]
        unique_index = conn.execute(
            "select [unique] from pragma_index_list('agent_sessions') where name = 'uq_agent_sessions_scope_anchor'"
        ).fetchone()
        media_scope_not_null = {
            row[1]: row[3] for row in conn.execute("pragma table_info(media_objects)")
        }["scope_id"]
        existing_media = conn.execute(
            "select scope_id, session_id from media_objects where token = 'media_existing'"
        ).fetchone()
        conn.execute(
            """
            insert into media_objects (
                token, scope_id, session_id, kind, source, local_path, created_at
            ) values (
                'media_standalone', null, 'ses_legacy_c', 'file',
                'user_upload', '/tmp/standalone.txt', ?
            )
            """,
            (now,),
        )

    assert rows == [
        ("ses_legacy_a", "scope_real", "ses_legacy_a", "background", "/legacy-a"),
        ("ses_legacy_b", "scope_real", "ses_legacy_b", "background", "/legacy-b"),
        ("ses_legacy_c", None, "ses_legacy_c", "background", "/legacy-c"),
    ]
    assert pseudo_scope_count == 3
    assert unique_index == (1,)
    assert media_scope_not_null == 0
    assert existing_media == ("scope_real", "ses_caller")

    from core.scheduled_tasks import resolve_session_id_target

    promoted = resolve_session_id_target("ses_legacy_a", db_path=db_path)
    assert promoted.scope_id == "scope_real"
    assert promoted.session_key.thread_id is None


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
        assert "session_id is not null" in _index_sql(
            conn, "ix_messages_inbox_activity"
        )
        assert "harness_dedupe" not in _index_sql(
            conn, "ix_messages_inbox_activity"
        )
        assert "author = 'harness'" in _index_sql(conn, "ix_messages_inbox_user_send")
    assert "ix_agent_sessions_scope_status_activity" in agent_session_indexes


#: The three ``agent_runs`` index migrations a head-shaped database must end up with,
#: whichever route it took there. Named by module so the DDL is only ever read from the
#: migration that owns it — the owed-notice index is an EXPRESSION index and SQLite only
#: matches an index expression against a byte-identical query expression, so a retyped
#: copy is an index that is built and silently ignored.
AGENT_RUNS_INDEX_MIGRATION_MODULES = (
    "storage.alembic.versions.20260728_0039_agent_runs_settled_at_index",
    "storage.alembic.versions.20260728_0041_agent_runs_owed_notice_backoff_index",
    "storage.alembic.versions.20260729_0042_agent_runs_definition_streak_index",
)
#: The superseded 0040 shape of ``ix_agent_runs_owed_notice``: same NAME, one fewer
#: indexed expression. Read from its own module for the same no-retyping reason.
SUPERSEDED_OWED_NOTICE_MIGRATION_MODULE = (
    "storage.alembic.versions.20260728_0040_agent_runs_owed_notice_index"
)
_LIVE_QUERY_NOW = "2026-07-27T12:00:00+00:00"
_LIVE_QUERY_DEFINITION = "task-repair"
#: The middle failure of the seeded streak: bracketed by a success on both sides, so
#: both of ``failure_streak_decision``'s boundary seeks have something to find.
_LIVE_QUERY_RUN = "run-0002"


def _agent_runs_index_names() -> tuple[str, str, str]:
    """``(settled, owed_notice, streak)`` index names, from the migrations themselves."""

    settled, owed_notice, streak = (
        import_module(module)._INDEX for module in AGENT_RUNS_INDEX_MIGRATION_MODULES
    )
    return settled, owed_notice, streak


def _assert_agent_runs_indexes_present(db_path: Path, *, route: str) -> None:
    names = _agent_runs_index_names()
    with sqlite3.connect(db_path) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'index' and name in (?, ?, ?)",
                names,
            )
        }
    assert present == set(names), (
        f"{route} left the agent_runs expression indexes missing: {sorted(set(names) - present)}"
    )


def _seed_settled_history(db_path: Path) -> None:
    """A realistic settled history for the three live two-second-tick reads.

    A failure streak closed by a success on either side, the earlier failure owing a
    pending notice, so the eligibility lookup, the health window and the streak read
    all have something to seek for. Written through the store rather than by hand so
    every column the real writers populate is populated here too.
    """

    from storage.background import SQLiteBackgroundTaskStore

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.upsert_scheduled_task(
            {
                "id": _LIVE_QUERY_DEFINITION,
                "name": _LIVE_QUERY_DEFINITION,
                "prompt": "go",
                "schedule_type": "cron",
                "cron": "0 * * * *",
                "enabled": True,
                "created_at": "2026-07-27T00:00:00+00:00",
                "updated_at": "2026-07-27T00:00:00+00:00",
            }
        )
        history = (
            ("run-0001", "succeeded"),
            (_LIVE_QUERY_RUN, "failed"),
            ("run-0003", "failed"),
            ("run-0004", "succeeded"),
        )
        for position, (run_id, status) in enumerate(history):
            instant = f"2026-07-27T{position + 1:02d}:00:00+00:00"
            store.enqueue_run(
                {
                    "id": run_id,
                    "request_type": "scheduled",
                    "status": status,
                    "definition_id": _LIVE_QUERY_DEFINITION,
                    "error": "boom" if status == "failed" else None,
                    "created_at": instant,
                    "completed_at": instant,
                    "metadata": (
                        {"owed_failure_notice": {"state": "pending", "attempts": 0}}
                        if run_id == _LIVE_QUERY_RUN
                        else {}
                    ),
                }
            )
    finally:
        store.close()


def _agent_runs_query_plan(store, db_path: Path, call) -> str:
    """Every ``agent_runs`` SELECT one call issues, with its plan, as one text."""

    from sqlalchemy import event

    captured: list[tuple[str, object]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "agent_runs" in statement and statement.strip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    event.listen(store.engine, "before_cursor_execute", _capture)
    try:
        call()
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture)
    assert captured, "the read issued no agent_runs SELECT"

    raw = sqlite3.connect(str(db_path))
    try:
        return "\n".join(
            str(row[-1])
            for statement, parameters in captured
            for row in raw.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        )
    finally:
        raw.close()


def _assert_live_reads_seek_agent_runs_indexes(db_path: Path, *, route: str) -> None:
    """The three live reads must SEEK these indexes on THIS database.

    Index existence is not the property that matters — the property is that the
    owed-notice drain, the health window and the streak read stop scanning run
    history. So this asserts the CONSTRAINED TERMS of the plan, not just the index
    name: a plan can name an index while the term stays a per-row filter (which is
    exactly what 0040 shipped and 0041 fixed).
    """

    from storage.background import SQLiteBackgroundTaskStore

    settled_index, owed_notice_index, streak_index = _agent_runs_index_names()
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        owed_plan = _agent_runs_query_plan(
            store,
            db_path,
            lambda: store.list_owed_failure_notices(limit=10, now=_LIVE_QUERY_NOW),
        )
        health_plan = _agent_runs_query_plan(
            store,
            db_path,
            lambda: store._health_rows([_LIVE_QUERY_DEFINITION], now=_LIVE_QUERY_NOW),
        )
        streak_plan = _agent_runs_query_plan(
            store,
            db_path,
            lambda: store.failure_streak_decision(_LIVE_QUERY_DEFINITION, _LIVE_QUERY_RUN),
        )
    finally:
        store.close()

    # The owed-notice eligibility seek: both expression terms constrained, and the
    # (created_at, id) order taken from the index so the LIMIT can short-circuit.
    assert f"USING INDEX {owed_notice_index} (<expr>=? AND <expr><?)" in owed_plan, (
        f"{route}: the owed-notice tick must seek {owed_notice_index} on both expression "
        f"terms; plan was:\n{owed_plan}"
    )
    assert "SCAN agent_runs" not in owed_plan, (
        f"{route}: the owed-notice tick must not scan run history; plan was:\n{owed_plan}"
    )
    assert "TEMP B-TREE" not in owed_plan, (
        f"{route}: the owed-notice order must come from the index; plan was:\n{owed_plan}"
    )

    # The health window: the per-definition seek is bounded by (definition_id, settled).
    assert f"USING INDEX {settled_index} (definition_id=? AND <expr>>?)" in health_plan, (
        f"{route}: the health window must seek {settled_index} on both terms; plan was:"
        f"\n{health_plan}"
    )
    assert "SCAN agent_runs" not in health_plan, (
        f"{route}: the health window must not scan run history; plan was:\n{health_plan}"
    )
    assert "TEMP B-TREE" not in health_plan, (
        f"{route}: the health window's newest-first order must come from the index; plan "
        f"was:\n{health_plan}"
    )

    # The streak's boundary seeks: the (created_at, id) row value has to be a real
    # index constraint, which only the streak index's third key can make it.
    compact_streak = streak_plan.replace(" ", "")
    assert streak_index in streak_plan, (
        f"{route}: the streak read must seek {streak_index}; plan was:\n{streak_plan}"
    )
    assert f"{streak_index}(definition_id=?AND(created_at,id)<(?,?))" in compact_streak, (
        f"{route}: the preceding success must be an indexed row-value seek; plan was:"
        f"\n{streak_plan}"
    )
    assert f"{streak_index}(definition_id=?AND(created_at,id)>(?,?))" in compact_streak, (
        f"{route}: the following success must be an indexed row-value seek; plan was:"
        f"\n{streak_plan}"
    )
    assert "SCAN agent_runs" not in streak_plan, (
        f"{route}: the streak read must not scan a definition's history; plan was:"
        f"\n{streak_plan}"
    )
    assert "TEMP B-TREE" not in streak_plan, (
        f"{route}: the streak's (created_at, id) order must come from the index; plan was:"
        f"\n{streak_plan}"
    )


def _head_shaped_unversioned_db(tmp_path: Path, name: str = "vibe.sqlite") -> Path:
    """A models-born database: head tables and columns, no ``alembic_version``.

    This is the shape ``metadata.create_all`` produces, which is what every
    ``background_tables_ready`` consumer accepts as ready — and it lacks all three
    ``agent_runs`` expression indexes, because ``storage/models.py`` cannot express
    them.
    """

    db_path = tmp_path / name
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as conn:
        for index_name in _agent_runs_index_names():
            conn.execute(f"drop index if exists {index_name}")
        conn.commit()
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None
        assert not {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type = 'index' and name in (?, ?, ?)",
                _agent_runs_index_names(),
            )
        }
    return db_path


@pytest.fixture(scope="module")
def reference_agent_runs_index_sql(tmp_path_factory):
    """The three indexes as a full 0001-to-head replay writes them, byte for byte."""

    reference_db = tmp_path_factory.mktemp("migration-index-reference") / "reference.sqlite"
    run_migrations(reference_db)
    with closing(sqlite3.connect(reference_db)) as conn:
        return MappingProxyType({name: _index_sql(conn, name) for name in _agent_runs_index_names()})


def test_head_shaped_stamp_replays_agent_runs_expression_indexes(tmp_path: Path, reference_agent_runs_index_sql) -> None:
    """The migration ENTRYPOINT leaves a head-shaped database with the 0039-0042 indexes.

    This test is GREEN FROM BIRTH — it is a refutation pin, not a fix; red-first does
    not apply. The refuted premise was that ``_stamp_existing_initial_schema`` stamps a
    head-shaped database "directly at ``LATEST_SCHEMA_REVISION``" and therefore never
    executes the owed-notice / settlement / streak index migrations, leaving the
    two-second owed-notice tick scanning run history. ``LATEST_SCHEMA_REVISION`` is
    ``20260622_0023``, which is the stamp FLOOR, not head: after that stamp
    ``_run_migrations_locked`` runs ``command.upgrade(cfg, "head")``, so 0024 through
    0042 — including all four index migrations — replay on exactly this path.

    That refutation is about the MECHANISM, not about the class. Two lanes reach a
    head-shaped database with no expression indexes and never come back through this
    entrypoint: any ``metadata.create_all`` consumer satisfies
    ``background_tables_ready`` (which checks tables and columns, never indexes), and
    the day ``LATEST_SCHEMA_REVISION`` moves past 0038 the stamp really would skip
    them. So ``_ensure_new_background_indexes`` now installs the same three indexes,
    sourced by import from the migrations that own the DDL rather than retyped — see
    ``test_head_schema_repair_installs_agent_runs_expression_indexes``, which drives the
    repair path alone. This test owns the REPLAY route, that one owns the REPAIR route,
    and both compare byte-for-byte against the same full-replay reference, so the two
    routes cannot converge on different SQL.
    """
    owed_notice_migration = import_module(AGENT_RUNS_INDEX_MIGRATION_MODULES[1])
    replayed_indexes = _agent_runs_index_names()

    # Reference: an empty path replays 0001 -> head with no stamping shortcut at all.
    reference_sql = reference_agent_runs_index_sql

    db_path = _head_shaped_unversioned_db(tmp_path)
    _seed_settled_history(db_path)

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        upgraded_sql = {name: _index_sql(conn, name) for name in replayed_indexes}

    assert version == (HEAD_REVISION,)
    assert upgraded_sql == reference_sql
    # The owed-notice eligibility and backoff expressions must be the migration's own,
    # never a retyped copy: the planner only matches byte-identical expression text.
    owed_notice_sql = upgraded_sql[owed_notice_migration._INDEX]
    assert owed_notice_migration._STATE_EXPR in owed_notice_sql
    assert owed_notice_migration._NEXT_ATTEMPT_EXPR in owed_notice_sql
    # Existing SQL is not the claim; a bounded read is. Proven on the database the
    # entrypoint actually produced.
    _assert_live_reads_seek_agent_runs_indexes(db_path, route="the migration entrypoint")


def test_head_schema_repair_installs_agent_runs_expression_indexes(tmp_path: Path, reference_agent_runs_index_sql) -> None:
    """The head-schema REPAIR path installs the 0039-0042 indexes by itself.

    ``_ensure_head_indexes`` promises a head-shaped database every index head has, and
    it silently lagged head by three: the settlement index, the owed-notice
    eligibility/backoff expression index and the definition-streak index were only ever
    created by their migrations. Two lanes make that a real gap. A database born from
    ``metadata.create_all`` satisfies ``background_tables_ready`` — tables and columns
    only, never indexes — so ``SQLiteBackgroundTaskStore`` accepts it as ready and
    ``initialize_background_tables`` never runs; and if ``LATEST_SCHEMA_REVISION`` ever
    moves past 0038, the stamp floor stops replaying them for everyone.

    Driven through ``_stamp_existing_initial_schema`` rather than through
    ``run_migrations``, and that choice is the whole point of the test: it is the only
    production caller that reaches ``_ensure_head_indexes`` on a head-shaped database,
    and it RETURNS after stamping the floor, leaving the database repaired-but-not-yet
    -upgraded. ``run_migrations`` would immediately replay 0024-0042 over the result and
    the assertions below could not tell which route created the indexes;
    ``_repair_head_required_columns`` reaches the same helper but only as a side effect
    of a column repair, so it would prove less about the index promise. The stamped
    revision is asserted to still be the FLOOR, which is what proves no upgrade ran.
    """
    settled_index, owed_notice_index, streak_index = _agent_runs_index_names()
    superseded = import_module(SUPERSEDED_OWED_NOTICE_MIGRATION_MODULE)
    reference_sql = reference_agent_runs_index_sql

    db_path = _head_shaped_unversioned_db(tmp_path)
    _seed_settled_history(db_path)
    # A survivor of the 0040 shape: same index NAME, one fewer indexed expression, so
    # the backoff term would stay a per-row filter. Repairing has to REBUILD it, not
    # leave it because the name is taken.
    with sqlite3.connect(db_path) as conn:
        # Seeding uses the production store, which now repairs the already-ready
        # schema on construction. Replace just this index afterwards to exercise
        # the lower-level head-schema repair route this test owns.
        conn.execute(f"drop index if exists {superseded._INDEX}")
        conn.execute(
            f"create index {superseded._INDEX} on agent_runs "
            f"({superseded._STATE_EXPR}, created_at, id)"
        )
        conn.commit()

    migrations._stamp_existing_initial_schema(db_path, migrations.alembic_config(db_path))

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert version == (migrations.LATEST_SCHEMA_REVISION,), (
        "this test is only about the repair path: a database stamped past the floor has "
        f"had migrations replayed over it, so it proves nothing; got {version}"
    )
    assert version != (HEAD_REVISION,)

    _assert_agent_runs_indexes_present(db_path, route="the head-schema repair path")
    with sqlite3.connect(db_path) as conn:
        repaired_sql = {
            name: _index_sql(conn, name)
            for name in (settled_index, owed_notice_index, streak_index)
        }
    # Byte-equal to the full replay, because the repair path executes the migrations'
    # own DDL rather than a copy of it. The owed-notice index in particular must be the
    # 0041 shape — the 0040 survivor seeded above has to be gone.
    assert repaired_sql == reference_sql
    _assert_live_reads_seek_agent_runs_indexes(db_path, route="the head-schema repair path")


def test_background_store_repairs_indexes_on_an_already_ready_schema(tmp_path: Path, reference_agent_runs_index_sql) -> None:
    """Store construction reaches index repair even when no migration is needed."""

    reference_sql = reference_agent_runs_index_sql
    db_path = _head_shaped_unversioned_db(tmp_path, "store-ready.sqlite")
    assert background_tables_ready(db_path)

    store = SQLiteBackgroundTaskStore(db_path)
    store.close()

    with sqlite3.connect(db_path) as conn:
        repaired_sql = {name: _index_sql(conn, name) for name in _agent_runs_index_names()}
        assert conn.execute("select name from sqlite_master where name = 'alembic_version'").fetchone() is None
    assert repaired_sql == reference_sql
    _assert_live_reads_seek_agent_runs_indexes(
        db_path,
        route="already-ready background store construction",
    )


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
        assert "session_id is not null" in _index_sql(conn, "ix_messages_inbox_activity")
        assert "harness_dedupe" not in _index_sql(conn, "ix_messages_inbox_activity")
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
    run_migrations(db_path, revision="20260729_0042")

    with sqlite3.connect(db_path) as conn:
        conn.execute("drop table alembic_version")
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
    run_migrations(db_path, revision="20260530_0009")

    with sqlite3.connect(db_path) as conn:
        assert "type" not in {row[1] for row in conn.execute("pragma table_info(messages)")}

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
        # This fixture stamps the database at 0002 and then exercises every later
        # migration. Current metadata has already replaced the 0004-era
        # ``show_pages.visibility`` shape, so remove the future tables instead of
        # presenting 0004 with an impossible hybrid schema.
        conn.execute("drop table show_page_access_entries")
        conn.execute("drop table show_pages")
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
    # Drop the process-local "already ensured" result so the second call really
    # replays the pipeline against the migrated database, the way the next
    # process to start will. Without the reset this would assert the
    # short-circuit (covered separately) instead of re-run idempotence.
    reset_ensured_sqlite_state()
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


def test_ensure_sqlite_state_short_circuits_after_first_success(
    tmp_path: Path, monkeypatch, hold_migration_lock_elsewhere
) -> None:
    # Callers put ensure_sqlite_state in front of ordinary operations (a login,
    # a read-only query, a skill delete), so a repeat call must cost nothing.
    # Re-running the pipeline per request serialized unrelated work on the
    # cross-process migration lock and turned transient SQLite contention into a
    # failed login (page: error=oauth_exchange_failed reason=OperationalError).
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"

    first = ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")

    migrations_run = 0
    real_run_migrations = importer.run_migrations

    def counting_run_migrations(*args, **kwargs):
        nonlocal migrations_run
        migrations_run += 1
        return real_run_migrations(*args, **kwargs)

    monkeypatch.setattr(importer, "run_migrations", counting_run_migrations)

    # An already-held migration lock is exactly the contention that failed the
    # login, and only another thread can hold it against this one: the lock is
    # re-entrant per path and thread, so taking it here would let the repeat call
    # take it again and pass whether or not it short-circuits. The call runs on
    # its own thread for the same reason the wait behind that lock is unbounded --
    # a regression has to fail this test rather than hang it.
    outcome: list = []

    def repeat_call() -> None:
        outcome.append(
            ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")
        )

    with hold_migration_lock_elsewhere(migration_lock_path_for(db_path)):
        caller = threading.Thread(target=repeat_call, daemon=True)
        caller.start()
        caller.join(30)
        assert not caller.is_alive(), "the repeat call queued behind the held migration lock"

    assert outcome, "the repeat call raised instead of short-circuiting"
    second = outcome[0]
    assert second is first
    assert migrations_run == 0

    # The guard is per target, not process-global: a different home still migrates.
    other_state_dir = tmp_path / "other"
    other_state_dir.mkdir()
    other = ensure_sqlite_state(
        db_path=other_state_dir / "vibe.sqlite",
        state_dir=other_state_dir,
        primary_platform="slack",
    )
    assert other.db_path != first.db_path
    assert migrations_run == 1

    # Both homes are migrated now, so nothing on disk distinguishes them any
    # more -- only the key does. A memo that is not per target would hand the
    # first home's caller the second home's report, with the wrong db_path and
    # the wrong counts, and no call would ever notice.
    assert ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack") is first
    assert migrations_run == 1

    # A database that disappeared is no longer ensured, whatever we remember.
    for suffix in ("", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    ensure_sqlite_state(db_path=db_path, state_dir=state_dir, primary_platform="slack")
    assert migrations_run == 2


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
    # Drop the process-local "already ensured" result so the second call really
    # replays the pipeline against the migrated database, the way the next
    # process to start will. Without the reset this would assert the
    # short-circuit (covered separately) instead of re-run idempotence.
    reset_ensured_sqlite_state()
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
                        "schedule_type": "at",
                        "run_at": "2026-05-15T01:00:00+00:00",
                        "timezone": "UTC",
                        "enabled": False,
                        "retired_at": "2026-05-15T01:00:00+00:00",
                        "retirement_reason": "schedule_consumed",
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
                        "enabled": False,
                        "retired_at": "2026-05-15T02:00:00+00:00",
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
            "select definition_type, session_id, legacy_session_key, retired_at, retirement_reason "
            "from run_definitions order by id"
        ).fetchall()
        runs = conn.execute("select id, run_type, status, session_id, error from agent_runs order by id").fetchall()
    assert tasks == [
        (
            "scheduled",
            "sesk8m4q2p7x",
            "slack::channel::C123",
            "2026-05-15T01:00:00+00:00",
            "schedule_consumed",
        ),
        (
            "watch",
            "sesk8m4q2p7x",
            "slack::channel::C123",
            "2026-05-15T02:00:00+00:00",
            None,
        ),
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


def test_media_reference_migration_derives_no_rows_from_existing_data(tmp_path: Path) -> None:
    """The revision creates the table. Nothing in the graph reads history to fill it.

    Seeded with one row of every shape the removed backfill consumed -- a media object
    carrying its own minting session, and an agent message in a *second* session linking
    that token -- and asserting the table comes out empty, rather than asserting two
    named rows are absent. A shape the scan would have matched cannot pass by going
    unlisted.

    Nothing replaced it, because the read path already covers what it wrote:
    media_service.register() records the reference when a token is minted or reused, and
    vibe/ui_server.py falls back to the media row's own session and scope when the
    reference set is empty, so a legacy attachment stays readable by the session that
    minted it either way.
    """
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260725_0035")
    now = "2026-07-25T00:00:00Z"

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            insert into scopes (
                id, platform, scope_type, native_id, native_type, is_private,
                supports_threads, metadata_json, first_seen_at, last_seen_at, updated_at
            ) values (
                'avibe::project::proj_media', 'avibe', 'project', 'proj_media', null,
                1, 1, '{}', ?, ?, ?
            )
            """,
            (now, now, now),
        )
        for session_id in ("ses_media_original", "ses_media_reuse"):
            conn.execute(
                """
                insert into agent_sessions (
                    id, scope_id, agent_backend, agent_variant, session_anchor,
                    native_session_id, status, visibility, metadata_json,
                    created_at, updated_at, last_active_at
                ) values (?, 'avibe::project::proj_media', 'codex', 'codex', ?, '',
                          'active', 'foreground', '{}', ?, ?, ?)
                """,
                (session_id, session_id, now, now, now),
            )
        conn.execute(
            """
            insert into media_objects (
                token, scope_id, session_id, kind, source, local_path, created_at
            ) values (
                'legacy-shared-token', 'avibe::project::proj_media',
                'ses_media_original', 'file', 'agent_reply', '/tmp/legacy.txt', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            insert into messages (
                id, scope_id, session_id, platform, author, type, source,
                content_text, content_json, metadata_json, created_at, updated_at
            ) values (
                'msg_legacy_media', 'avibe::project::proj_media', 'ses_media_reuse',
                'avibe', 'agent', 'result', 'agent',
                'See [attachment](/api/media/legacy-shared-token)', '{}', '{}', ?, ?
            )
            """,
            (now, now),
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        references = set(
            conn.execute(
                "select token, session_id from media_object_references"
            )
        )
        version = conn.execute("select version_num from alembic_version").fetchone()

    assert references == set()
    assert version == (HEAD_REVISION,)


def test_background_tables_ready_requires_project_acl_and_media_references(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision="20260725_0035")

    assert background_tables_ready(db_path) is False

    run_migrations(db_path)

    assert background_tables_ready(db_path) is True


def test_run_migrations_upgrades_released_0030_to_acl_head(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"

    run_migrations(db_path, revision="20260707_0029")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "update messages set author = 'harness', type = 'harness' "
            "where source = 'harness' and author = 'user' and type = 'user'"
        )
        conn.execute("drop index if exists ix_messages_inbox_user_send")
        conn.execute(
            "create index ix_messages_inbox_user_send "
            "on messages (platform, session_id, created_at desc, id desc) "
            "where session_id is not null and "
            "((author = 'user' and type = 'user') or "
            "(author = 'harness' and type = 'harness'))"
        )
        conn.execute(
            "update alembic_version set version_num = '20260716_0030'"
        )
        conn.commit()

    run_migrations(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("select version_num from alembic_version").fetchone()
        tables = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        assert version == (HEAD_REVISION,)
        assert build_partial_index_predicate("ix_messages_inbox_activity") in _index_sql(
            conn,
            "ix_messages_inbox_activity",
        )
        assert "resource_access_policies" in tables
        assert "resource_access_groups" in tables


def _initial_era_db(db_path: Path, tables: Iterable[str], stamp: str = migrations.INITIAL_REVISION) -> Path:
    """A database stamped at ``stamp`` carrying ``tables``, each holding one row.

    The tables are placeholders on purpose. What decides whether the reset below fires is
    which names are present and what the stamp says, so a faithful copy of any particular
    release's DDL would only make the fixture look more specific than the thing it tests.
    The rows are there so a drop is visible as lost data rather than as a lost empty shell.
    """

    with sqlite3.connect(db_path) as conn:
        conn.execute("create table alembic_version (version_num varchar(32) not null)")
        conn.execute("insert into alembic_version (version_num) values (?)", (stamp,))
        for table in tables:
            conn.execute(f'create table "{table}" (id text primary key)')
            conn.execute(f'insert into "{table}" (id) values (?)', (f"row-of-{table}",))
        conn.commit()
    return db_path


def _surviving_tables(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        names = [
            str(name)
            for (name,) in conn.execute(
                "select name from sqlite_master where type = 'table' and name not like 'sqlite_%'"
            )
        ]
        return {name: conn.execute(f'select count(*) from "{name}"').fetchone()[0] for name in names}


@pytest.mark.parametrize("absent", sorted(migrations.INITIAL_TABLES))
def test_the_initial_drift_reset_never_touches_a_released_database(tmp_path: Path, absent: str) -> None:
    """A database carrying only released tables is not drift, however incomplete it is.

    The reset only runs at all once INITIAL_TABLES is not a subset of what the database
    carries, which sounds like a shape no release ships and is not: the initial migration
    has gained tables since it was first released, so a database from an older release is
    stamped at INITIAL_REVISION and short of today's set. That is a released database, and
    the reset dropping `scopes` and the stamp out of it is what made every v2.3-era
    upgrade abort. The cases come from INITIAL_TABLES itself, so a table added to it later
    is covered without editing this test, and one absence is the general case because the
    reset decides once for the database and then drops table by table.
    """

    present = sorted(migrations.INITIAL_TABLES - {absent})
    db_path = _initial_era_db(tmp_path / f"without-{absent}.sqlite", present)
    before = _surviving_tables(db_path)

    migrations._reset_unreleased_initial_schema_drift(db_path)

    assert _surviving_tables(db_path) == before


@pytest.mark.parametrize("drifted", migrations.UNRELEASED_ONLY_INITIAL_TABLES)
def test_the_initial_drift_reset_still_clears_an_unreleased_database(tmp_path: Path, drifted: str) -> None:
    """Every table the reset treats as evidence of an unreleased build has to be evidence.

    Parametrized over the list the reset reads rather than over a sample of it, so a member
    that stops firing -- or a list narrowed until nothing does -- fails here instead of
    leaving a dev database to be upgraded as if it were a released one. Seeded with a
    released database's tables as well, because the drifted shape carries both and the
    reset is meant to clear it regardless.
    """

    db_path = _initial_era_db(
        tmp_path / f"drifted-{drifted}.sqlite",
        [*sorted(migrations.INITIAL_TABLES - {"agents"}), drifted],
    )

    migrations._reset_unreleased_initial_schema_drift(db_path)

    surviving = _surviving_tables(db_path)
    assert drifted not in surviving
    assert "alembic_version" not in surviving


def test_legacy_sessions_import_preflight_matches_the_migration_on_every_key_shape(
    tmp_path: Path,
) -> None:
    """The preflight must require a platform exactly when the migration needs one.

    ``_migrate_session_state_for_import`` decides whether ``primary_platform``
    is mandatory, then hands the state to ``migrate_session_state_mappings``.
    Two independent copies of "is this a legacy raw key?" can disagree, and the
    failure is asymmetric: the preflight refuses to import state the migration
    would have left untouched, so a user with that state cannot start at all.
    Seed one key of every shape rather than listing the ones that are exempt.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "session_mappings": {
                    # A Session with no Scope: no platform in the key and none
                    # inferable from the anchors, yet not legacy.
                    "": {"codex": {"archived:seed": "codex-session-scopeless"}},
                    # Already prefixed.
                    "slack::C123": {"codex": {"slack_1774074591.762089": "codex-session-1"}},
                    # Legacy, but the platform is inferable from the anchors.
                    "D456": {"codex": {"discord_1485641561998889093:/repo": "codex-session-2"}},
                    # Legacy key with nothing left in it.
                    "C999": {},
                }
            }
        ),
        encoding="utf-8",
    )

    report = ensure_sqlite_state(db_path=db_path, state_dir=state_dir)

    assert report.imported is True
    with sqlite3.connect(db_path) as conn:
        anchors = {
            row[0]
            for row in conn.execute("select session_anchor from agent_sessions").fetchall()
        }
    assert "archived:seed" in anchors, "the scope-less Session was dropped by the import"


def test_legacy_sessions_import_still_requires_platform_for_an_unresolvable_legacy_key(
    tmp_path: Path,
) -> None:
    """Exempting the empty key must not exempt a real legacy key beside it."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    (state_dir / "sessions.json").write_text(
        json.dumps(
            {
                "session_mappings": {
                    "": {"codex": {"archived:seed": "codex-session-scopeless"}},
                    "C123": {"codex": {"1774074591.762089:/repo": "codex-session-1"}},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="primary_platform is required"):
        ensure_sqlite_state(db_path=db_path, state_dir=state_dir)
