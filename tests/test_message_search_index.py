"""Search acceleration cannot change results, authority, or old-writer behavior.

The previous LIKE-only suite never initializes an index or crosses a committed
backfill boundary. These tests own that boundary using real SQLite, including
partial progress, pending writes, rollback/crash recovery and fixture downgrade.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from threading import Event

from alembic import command
import pytest
from sqlalchemy import event

from storage import message_search_index as index, messages_service, migrations
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.migration_policy import classify_migration, classify_migration_source
from storage.models import messages, scope_settings
from tests.test_message_search import _insert_msg, _seed_scope, _seed_session

MIGRATION = migrations.alembic_dir() / "versions/20261112_0084_message_search_journal.py"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    result = create_sqlite_engine()
    yield result
    result.dispose()


def drain(engine):
    for _ in range(100):
        status = index.run_batch(engine)
        if status == "idle":
            return
        assert status == "pending"
    pytest.fail("small hermetic fixture did not finish indexing")


def seed(engine, count=40):
    with engine.begin() as conn:
        scope = _seed_scope(conn)
        _seed_session(conn, scope, "search-session")
        for number in range(count):
            _insert_msg(conn, scope, "search-session", "user", f"watch item {number}",
                        "2026-11-01T00:00:00Z", msg_id=f"m{number:04d}")
    return scope


def search(engine, query="watch", **kwargs):
    with engine.connect() as conn:
        return messages_service.search_messages(conn, query=query, **kwargs)


def ids(result):
    return [match["id"] for session in result["sessions"] for match in session["matches"]]


def test_backfill_partial_restart_and_pending_writes_keep_complete_results(engine):
    scope = seed(engine)
    expected = search(engine)
    assert index.run_batch(engine) == "pending"
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT ready FROM message_search_state").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_documents").scalar_one() == index.BATCH_SIZE
    assert search(engine) == expected
    engine.dispose()  # Reopen connections, with no process-local index state.
    with engine.begin() as conn:
        conn.execute(messages.update().where(messages.c.id == "m0001").values(content_text="newneedle"))
        conn.execute(messages.delete().where(messages.c.id == "m0002"))
        _insert_msg(conn, scope, "search-session", "user", "newneedle watch", "2026-11-02T00:00:00Z", msg_id="a-before-cursor")
    before = search(engine)
    drain(engine)
    assert search(engine) == before
    assert set(ids(search(engine, "newneedle"))) == {"m0001", "a-before-cursor"}
    # An old binary writes without any index-specific application hooks.
    with engine.begin() as conn:
        conn.execute(messages.update().where(messages.c.id == "m0003").values(content_text="freshneedle"))
        conn.execute(messages.delete().where(messages.c.id == "m0004"))
        _insert_msg(conn, scope, "search-session", "user", "freshneedle", "2026-11-03T00:00:00Z", msg_id="latest")
    pending = search(engine, "freshneedle")
    assert set(ids(pending)) == {"m0003", "latest"}
    assert "m0004" not in ids(search(engine))
    drain(engine)
    assert search(engine, "freshneedle") == pending
    # Stable document ids, unlike messages.rowid, survive a VACUUM.
    with engine.connect() as conn:
        conn.exec_driver_sql("VACUUM")
    assert search(engine, "freshneedle") == pending


def test_accelerated_results_match_legacy_semantics_and_all_access_filters(engine):
    with engine.begin() as conn:
        allowed = _seed_scope(conn, "allowed")
        denied = _seed_scope(conn, "denied")
        archived_project = _seed_scope(conn, "archived-project")
        conn.execute(scope_settings.update().where(scope_settings.c.scope_id == archived_project).values(enabled=0))
        for session, scope, status, visibility in (
            ("active", allowed, "active", "foreground"),
            ("archived", allowed, "archived", "foreground"),
            ("hidden", allowed, "active", "background"),
            ("system", allowed, "active", "system"),
            ("denied", denied, "active", "foreground"),
            ("disabled", archived_project, "active", "foreground"),
        ):
            _seed_session(conn, scope, session, status=status, visibility=visibility)
            for number, content in enumerate((
                "WaTcH watcher 50% value_x slash\\thing a\"b部署测试 École école",
                "a b a-b a_b AND OR NOT NEAR star* percent%",
                "watch late acceptance", "watch in tool output", "watch on another platform",
                "NUL\x00watch", "emoji 😀😃😄 punctuation...",
            )):
                _insert_msg(conn, scope, session, "user", content, f"2026-11-01T00:00:0{number}Z",
                            msg_id=f"{session}-{number}", msg_type="assistant" if number == 3 else "user",
                            platform="slack" if number == 4 else "avibe",
                            delivered_at="2026-11-04T00:00:00Z" if number == 2 else None)
    queries = ["watch", "atch", "50%", "value_x", "slash\\thing", 'a"b', "部署", "部署测试", "Éco", "éco",
               "a b", "a-b", "a_b", "AND", "OR", "NOT NEAR", "star*", "percent%", "😀😃😄", "...", "NUL\x00watch"]
    options = [{}, {"scope_ids": [allowed]}, {"scope_ids": []}, {"include_archived": True},
               {"hidden_session_ids": ["active"]}, {"platform": "slack"}, {"types": ["assistant"]},
               {"types": []}, {"limit": 2}]
    expected = {(query, number): search(engine, query, **kwargs)
                for query in queries for number, kwargs in enumerate(options)}
    drain(engine)
    for query in queries:
        for number, kwargs in enumerate(options):
            assert search(engine, query, **kwargs) == expected[query, number], (query, kwargs)
    assert set(ids(search(engine, "watch", scope_ids=[allowed]))) == {"active-0", "active-2"}


def test_index_query_plan_uses_fts_and_keyed_document_lookup(engine):
    seed(engine)
    drain(engine)
    plans = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "message_search_fts MATCH" in statement and not statement.startswith("EXPLAIN"):
            plans.extend(row[3] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert search(engine)["total"] == 40
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert any("message_search_fts VIRTUAL TABLE INDEX" in line for line in plans)
    assert any("SEARCH d USING INTEGER PRIMARY KEY" in line for line in plans)
    assert any("SEARCH messages USING INDEX sqlite_autoindex_messages_1 (id=?)" in line for line in plans), "\n".join(plans)


def test_cancelled_batch_rolls_back_index_cursor_and_acknowledgements(engine):
    seed(engine)
    cancel = Event()

    def cancel_after_index_write(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO message_search_fts"):
            cancel.set()

    event.listen(engine, "after_cursor_execute", cancel_after_index_write)
    try:
        assert index.run_batch(engine, cancel_event=cancel) == "cancelled"
    finally:
        event.remove(engine, "after_cursor_execute", cancel_after_index_write)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_dirty").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_state").scalar_one() == 0
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
    drain(engine)
    assert search(engine)["total"] == 40


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_before_or_after_batch_commit_is_resumable(engine, after_commit):
    seed(engine)
    code = """
import os, sys
from sqlalchemy import event
from storage.db import create_sqlite_engine
from storage.message_search_index import run_batch
engine = create_sqlite_engine()
if sys.argv[1] == 'before':
    def crash(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO message_search_fts'):
            os._exit(23)
    event.listen(engine, 'after_cursor_execute', crash)
run_batch(engine)
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", code, "after" if after_commit else "before"],
                            cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(), timeout=30)
    assert result.returncode == 23
    with engine.connect() as conn:
        count = conn.exec_driver_sql("SELECT count(*) FROM message_search_documents").scalar_one()
        assert count == (index.BATCH_SIZE if after_commit else 0)
    drain(engine)
    assert search(engine)["total"] == 40


def test_no_fts_module_falls_back_without_affecting_ledger_writes(engine):
    from sqlalchemy.exc import OperationalError

    seed(engine)

    def missing_module(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE VIRTUAL TABLE"):
            raise OperationalError(statement, parameters, sqlite3.OperationalError("no such module: fts5"))

    event.listen(engine, "before_cursor_execute", missing_module)
    try:
        assert index.run_batch(engine) == "unsupported"
        with engine.begin() as conn:
            conn.execute(messages.update().where(messages.c.id == "m0001").values(content_text="still writable watch"))
        assert search(engine)["total"] == 40
    finally:
        event.remove(engine, "before_cursor_execute", missing_module)
    drain(engine)
    assert search(engine)["total"] == 40


@pytest.mark.no_sqlite_template
def test_upgrade_is_empty_additive_and_downgrade_preserves_old_writes(tmp_path):
    path = tmp_path / "fixture.sqlite"
    migrations.run_migrations(path, revision="20261108_0083")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO messages (id,platform,author,type,content_text,content_json,metadata_json,created_at,updated_at) "
                     "VALUES ('old','avibe','user','user','watch','{}','{}','now','now')")
        original = conn.execute("SELECT * FROM messages").fetchall()
    migrations.run_migrations(path, revision="20261112_0084")
    assert classify_migration(MIGRATION).permitted == "additive"
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT * FROM messages").fetchall() == original
        assert conn.execute("SELECT count(*) FROM message_search_documents").fetchone() == (0,)
        assert conn.execute("SELECT name FROM sqlite_schema WHERE name='message_search_fts'").fetchone() is None
        assert conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone() == ("2",)
        # Index activation is deferred until the first successful worker batch.
        assert conn.execute("SELECT count(*) FROM message_search_dirty").fetchone() == (0,)
        conn.execute("INSERT INTO message_search_state(id) VALUES (1)")
        # Named-column old writers and id changes stay valid without any FTS module.
        conn.execute("UPDATE messages SET content_text='changed' WHERE id='old'")
        conn.execute("UPDATE messages SET id='renamed' WHERE id='old'")
        conn.execute("DELETE FROM messages WHERE id='renamed'")
        assert set(conn.execute("SELECT message_id FROM message_search_dirty")) == {("old",), ("renamed",)}
    command.downgrade(migrations.alembic_config(path), "20261108_0083")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'message_search_%'").fetchall() == []
        assert conn.execute("SELECT * FROM messages").fetchall() == []
    migrations.run_migrations(path, revision="20261112_0084")


@pytest.mark.parametrize("old,new", [
    ("INSERT OR IGNORE INTO message_search_dirty", "INSERT INTO message_search_dirty"),
    ("VALUES (new.id)", "VALUES (new.content_text)"),
    ("AFTER INSERT ON messages", "BEFORE INSERT ON messages"),
    ("sa.Text(), primary_key=True, nullable=False", "sa.Integer(), primary_key=True, nullable=False"),
    ("sa.Text(), primary_key=True, nullable=False", "sa.Text(), primary_key=True, nullable=False, server_default='x'"),
    ("END\"\"\")", "DELETE FROM messages; END\"\"\")"),
    ('"message_search_dirty",', '"another_table",'),
])
def test_additive_journal_proof_rejects_changes_to_write_contract(old, new):
    source = MIGRATION.read_text()
    assert old in source
    assert classify_migration_source(source.replace(old, new)).permitted == "breaking"


def test_oversized_messages_never_tokenize_unbounded_text_or_lose_suffix_matches(engine):
    seed(engine, count=1)
    with engine.begin() as conn:
        conn.execute(messages.update().values(content_text="x" * (index.MAX_INDEX_CHARACTERS + 1) + " suffixneedle"))
    expected = search(engine, "suffixneedle")
    assert expected["total"] == 1
    drain(engine)
    assert search(engine, "suffixneedle") == expected
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT oversized FROM message_search_documents").scalar_one() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_fts").scalar_one() == 0
    with engine.begin() as conn:
        conn.execute(messages.update().values(content_text="suffixneedle now short"))
    drain(engine)
    assert search(engine, "suffixneedle")["total"] == 1
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT oversized FROM message_search_documents").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_fts").scalar_one() == 1


def test_unsupported_version_deactivates_journal_then_rebuilds_after_old_writes(engine):
    seed(engine)
    drain(engine)
    with engine.begin() as conn:
        conn.execute(messages.update().values(content_text="changed watch"))

    def older_version(conn, cursor, statement, parameters, context, executemany):
        return ("SELECT '3.42.0'" if statement == "SELECT sqlite_version()" else statement), parameters

    event.listen(engine, "before_cursor_execute", older_version, retval=True)
    try:
        assert index.run_pass(engine) == "unsupported"
        with engine.begin() as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM message_search_state").scalar_one() == 0
            assert conn.exec_driver_sql("SELECT count(*) FROM message_search_dirty").scalar_one() == 0
            conn.execute(messages.update().where(messages.c.id == "m0001").values(content_text="oldwriterneedle"))
            conn.execute(messages.delete().where(messages.c.id == "m0002"))
            assert conn.exec_driver_sql("SELECT count(*) FROM message_search_dirty").scalar_one() == 0
        assert ids(search(engine, "oldwriterneedle")) == ["m0001"]
        assert "m0002" not in ids(search(engine))
    finally:
        event.remove(engine, "before_cursor_execute", older_version)
    engine.dispose()
    # A capable runtime must not reuse an earlier ready marker/index.
    assert ids(search(engine, "oldwriterneedle")) == ["m0001"]
    drain(engine)
    assert ids(search(engine, "oldwriterneedle")) == ["m0001"]
    assert "m0002" not in ids(search(engine))


def test_index_failure_log_never_contains_search_text_or_sql_parameters(engine, caplog):
    from sqlalchemy.exc import DatabaseError

    private_query = "PRIVATE_QUERY_TOKEN_XY17"
    seed(engine, count=1)
    with engine.begin() as conn:
        conn.execute(messages.update().values(content_text=private_query))
    drain(engine)

    def index_error(conn, cursor, statement, parameters, context, executemany):
        if "message_search_fts MATCH" in statement:
            # Both SQLite's message and SQLAlchemy's parameters may contain
            # private query material; neither belongs in diagnostic logs.
            raise DatabaseError(statement, parameters, sqlite3.DatabaseError(f"index failed for {private_query}"))

    event.listen(engine, "before_cursor_execute", index_error)
    try:
        assert search(engine, private_query)["total"] == 1
    finally:
        event.remove(engine, "before_cursor_execute", index_error)
    assert "retrying authoritative LIKE search" in caplog.text
    assert private_query not in caplog.text
    assert "message_search_fts MATCH" not in caplog.text
    assert '"pri"' not in caplog.text


def test_corrupt_ready_index_falls_back_to_same_authorized_results(engine, caplog):
    scope = seed(engine)
    expected = search(engine, scope_ids=[scope], hidden_session_ids=[])
    drain(engine)
    # Damage only the derived index, not the authoritative ledger.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE message_search_fts_data")
    engine.dispose()
    assert search(engine, scope_ids=[scope], hidden_session_ids=[]) == expected
    assert search(engine, scope_ids=["not-readable"])["total"] == 0
    assert "retrying authoritative LIKE search" in caplog.text


@pytest.mark.no_sqlite_template
def test_head_shaped_unversioned_schema_replays_search_migration(tmp_path):
    from storage.models import metadata

    path = tmp_path / "head-shape.sqlite"
    engine = create_sqlite_engine(path)
    try:
        metadata.create_all(engine)
        migrations.run_migrations(path)
        with engine.begin() as conn:
            triggers = conn.exec_driver_sql(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'message_search_%'"
            ).all()
            assert len(triggers) == 3
            assert conn.exec_driver_sql("SELECT count(*) FROM message_search_state").scalar_one() == 0
        seed(engine, count=1)
        drain(engine)
        with engine.begin() as conn:
            conn.execute(messages.update().values(content_text="replayedneedle"))
        assert search(engine, "replayedneedle")["total"] == 1
    finally:
        engine.dispose()


def test_search_snapshot_preserves_caller_rollback_and_read_only_connections(engine):
    seed(engine, count=1)
    drain(engine)
    with engine.connect() as conn:
        conn.execute(messages.update().values(content_text="uncommittedneedle"))
        assert messages_service.search_messages(conn, query="uncommittedneedle")["total"] == 1
        conn.rollback()
    assert search(engine, "uncommittedneedle")["total"] == 0
    read_only = create_sqlite_engine(Path(engine.url.database), read_only=True)
    try:
        with read_only.connect() as conn:
            assert messages_service.search_messages(conn, query="watch")["total"] == 1
    finally:
        read_only.dispose()


def test_readiness_and_candidate_query_share_a_snapshot(engine):
    seed(engine, count=2)
    with engine.begin() as conn:
        conn.execute(messages.update().where(messages.c.id == "m0000").values(content_text="needle"))
        conn.execute(messages.update().where(messages.c.id == "m0001").values(content_text="other"))
    drain(engine)
    switched = False

    def deactivate_between_reads(conn, cursor, statement, parameters, context, executemany):
        nonlocal switched
        if statement == "SELECT rowid FROM message_search_fts LIMIT 0" and not switched:
            switched = True
            with engine.begin() as writer:
                writer.exec_driver_sql("DELETE FROM message_search_state")
                writer.exec_driver_sql("DELETE FROM message_search_dirty")
                writer.execute(messages.update().where(messages.c.id == "m0000").values(content_text="other"))
                writer.execute(messages.update().where(messages.c.id == "m0001").values(content_text="needle"))

    event.listen(engine, "before_cursor_execute", deactivate_between_reads)
    try:
        # Both committed states have exactly one hit. Mixing old index readiness
        # with new ledger contents would incorrectly return none.
        assert search(engine, "needle")["total"] == 1
        assert switched
    finally:
        event.remove(engine, "before_cursor_execute", deactivate_between_reads)
    assert ids(search(engine, "needle")) == ["m0001"]


def test_busy_writer_defers_batch_and_restores_pool_timeout(engine):
    seed(engine)
    with engine.connect() as writer:
        writer.exec_driver_sql("BEGIN IMMEDIATE")
        assert index.run_batch(engine) == "pending"
        writer.rollback()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
        assert conn.exec_driver_sql("SELECT count(*) FROM message_search_state").scalar_one() == 0
    drain(engine)
    assert search(engine)["total"] == 40
