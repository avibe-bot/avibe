"""The Turn's last delivered reply (docs/plans/reply-and-turn-end.md, rule 5).

Scenario: MESSAGE-DELIVERY-335. ``session_turn_replies`` holds, per Turn, the Run copy of
the last reply the dispatcher delivered; the dispatcher writes it on delivery and reads it
at a content-free Turn end. It is a table of its own: the write is one upsert keyed by the
Turn's id, never bumps the Turn's ``version``, and a transition that races it still lands.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import event, select

from config import paths
from core.message_output import terminal_turn_output
from storage import message_deliveries, migrations
from storage.db import create_sqlite_engine, dispose_cached_sqlite_engines
from storage.importer import ensure_sqlite_state
from storage.models import session_turn_replies
from storage.settings_service import upsert_scope

NOW = "2026-10-20T00:00:00.000000Z"
SESSION = "ses_last_reply"
TURN = "trn_last_reply"
PREVIOUS_REVISION = "20261018_0070"
HEAD = ScriptDirectory.from_config(migrations.alembic_config()).get_current_head()


def _seed(conn) -> dict:
    """A Session and one Turn claimed through the real Delivery path, still ``starting``."""
    scope_id = upsert_scope(conn, platform="avibe", scope_type="project", native_id="proj_last_reply", now=NOW)
    conn.exec_driver_sql(
        "insert into agent_sessions (id, scope_id, agent_name, agent_backend, agent_variant, session_anchor, "
        "workdir, native_session_id, status, visibility, pinned, agent_status, metadata_json, created_at, "
        "updated_at, last_active_at) values (?, ?, 'codex', 'codex', 'codex', ?, '/tmp', ?, 'active', "
        "'foreground', 0, 'idle', ?, ?, ?, ?)",
        (SESSION, scope_id, SESSION, SESSION, json.dumps({}), NOW, NOW, NOW),
    )
    delivery = message_deliveries.insert_delivery(
        conn,
        delivery_id="dlv_last_reply",
        session_id=SESSION,
        priority="p3",
        state="reserved",
        snapshot=message_deliveries.message_snapshot(
            scope_id=scope_id,
            session_id=SESSION,
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text="is the cache safe?",
        ),
        dispatch_text="is the cache safe?",
        now=NOW,
    )
    claimed = message_deliveries.claim_start_batch(
        conn, turn_id=TURN, session_id=SESSION, backend="codex", deliveries=[delivery], dispatch_text="is it?"
    )
    return claimed["turn"]


def test_a_reply_write_and_a_turn_transition_both_land():
    """MESSAGE-DELIVERY-335: a reply recorded between a transition's read and its
    compare-and-set does not make the transition lose, and the transition neither writes
    nor clears the reply; once the Turn is no longer live, nothing more is recorded."""
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    try:
        with engine.begin() as conn:
            starting = _seed(conn)
            assert starting["state"] == "starting"
            # The reply lands after the activation read the Turn's version...
            assert message_deliveries.record_turn_reply(conn, TURN, "The cache is safe.")
            assert message_deliveries.get_turn(conn, TURN)["version"] == starting["version"]
            # ...and the activation's compare-and-set on that version still succeeds.
            active = message_deliveries.bind_native_start(
                conn,
                TURN,
                expected_version=int(starting["version"]),
                runtime_key="runtime",
                runtime_turn_id=TURN,
                native_turn_id=TURN,
            )
            assert active is not None and active["state"] == "active"
            assert message_deliveries.turn_last_reply(conn, TURN) == "The cache is safe."

            # Repeated replies: the last delivered one wins, identical text included.
            for reply in ("The lock is fine too.", "The cache is safe."):
                assert message_deliveries.record_turn_reply(conn, TURN, reply)
            settled = message_deliveries.terminalize_turn(
                conn, TURN, outcome="completed", settled_by="terminal_result", evidence_kind="test"
            )
            assert settled["changed"] and settled["turn"]["state"] == "terminal"
            assert message_deliveries.turn_last_reply(conn, TURN) == "The cache is safe."

            # A Turn that ended keeps what it ended with; one that never existed has none.
            assert not message_deliveries.record_turn_reply(conn, TURN, "Too late.")
            assert not message_deliveries.record_turn_reply(conn, "trn_unknown", "Nobody's.")
            assert message_deliveries.turn_last_reply(conn, TURN) == "The cache is safe."
            assert message_deliveries.turn_last_reply(conn, "trn_unknown") is None
            assert conn.execute(select(session_turn_replies.c.turn_id)).scalars().all() == [TURN]
    finally:
        engine.dispose()


def test_the_reply_write_and_read_are_primary_key_searches():
    """MESSAGE-DELIVERY-335: both run on every reply and every Turn end; each touches one
    row by primary key, the write's live-Turn guard included."""
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    statements: list[tuple[str, tuple]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "session_turn_replies" in statement:
            statements.append((statement, parameters))

    try:
        with engine.begin() as conn:
            _seed(conn)
        event.listen(engine, "before_cursor_execute", capture)
        with engine.begin() as conn:
            message_deliveries.record_turn_reply(conn, TURN, "The cache is safe.")
            message_deliveries.turn_last_reply(conn, TURN)
        event.remove(engine, "before_cursor_execute", capture)
        assert [sql.split()[0] for sql, _ in statements] == ["INSERT", "SELECT"]
        with engine.connect() as conn:
            plans = [
                [row[-1] for row in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)]
                for sql, params in statements
            ]
    finally:
        engine.dispose()
    assert plans == [
        ["SEARCH session_turns USING INDEX sqlite_autoindex_session_turns_1 (id=?)"],
        ["SEARCH session_turn_replies USING INDEX sqlite_autoindex_session_turn_replies_1 (turn_id=?)"],
    ], plans


class _Pre335Connection(sqlite3.Connection):
    """SQLite before 3.35, which has no ``DROP COLUMN``."""

    def cursor(self, factory=None):
        cursor = super().cursor(factory or _Pre335Cursor)
        return cursor


class _Pre335Cursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        if " DROP COLUMN " in f" {' '.join(str(sql).upper().split())} ":
            raise sqlite3.OperationalError('near "DROP": syntax error')
        return super().execute(sql, parameters)


@pytest.mark.no_sqlite_template
@pytest.mark.parametrize("sqlite_drops_columns", [True, False], ids=["sqlite", "sqlite-before-3.35"])
async def test_a_released_database_upgrades_and_its_running_turn_ends_without_a_reply(
    tmp_path, monkeypatch, sqlite_drops_columns
):
    """MESSAGE-DELIVERY-335: a database a released version wrote (no table) upgrades with its
    running Turn intact; that Turn has no reply on record, so its content-free end settles
    its Run with "". Downgrade drops the table, on SQLite before 3.35 too, and the database
    upgrades again."""
    from core.scheduled_tasks import TaskExecutionStore
    from tests.test_message_dispatcher_turn_end import _Controller

    if not sqlite_drops_columns:
        real_connect = sqlite3.connect

        def legacy_connect(*args, **kwargs):
            kwargs["factory"] = _Pre335Connection
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", legacy_connect)
        monkeypatch.setattr(sqlite3.dbapi2, "connect", legacy_connect)
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    dispose_cached_sqlite_engines()
    path = paths.get_sqlite_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations.run_migrations(path, revision=PREVIOUS_REVISION)
    # The released shape, a running Turn written as a released version writes it (its
    # Session and Delivery rows are not what this migration is about, so no foreign keys).
    with sqlite3.connect(path) as conn:
        assert "session_turn_replies" not in {
            row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")
        }
        conn.execute(
            "insert into session_turns (id, session_id, initial_delivery_id, state, backend, "
            "start_attempt_id, start_receipt_outcome, dispatch_text, dispatch_sha256, version, "
            "created_at, updated_at) values (?, ?, ?, 'active', 'codex', 'atm_released', 'accepted', "
            "'is it?', 'sha', 3, ?, ?)",
            (TURN, SESSION, "dlv_released", NOW, NOW),
        )

    try:
        ensure_sqlite_state()
        engine = create_sqlite_engine(path)
        try:
            with engine.connect() as conn:
                assert message_deliveries.turn_last_reply(conn, TURN) is None
                turn = message_deliveries.get_turn(conn, TURN)
                assert (turn["state"], turn["version"]) == ("active", 3)
        finally:
            engine.dispose()
        runs = TaskExecutionStore()
        run = runs.enqueue_task_run("task-released")
        assert runs.claim(run.id) is not None
        controller = _Controller("avibe")
        context = controller_context(run.id)
        await controller.emit_agent_message(context, "result", "", output=terminal_turn_output())
        [snapshot] = controller.snapshots
        assert snapshot["terminal_evidence"]["result_text"] == ""
        settled = runs.get_run(run.id)
        assert (settled["status"], settled["result_text"] or "") == ("succeeded", "")
    finally:
        dispose_cached_sqlite_engines()

    with sqlite3.connect(path) as conn:
        assert conn.execute("select version_num from alembic_version").fetchone() == (HEAD,)
    command.downgrade(migrations.alembic_config(path), PREVIOUS_REVISION)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
        assert "session_turn_replies" not in tables
        assert conn.execute("select state, version from session_turns where id = ?", (TURN,)).fetchone() == (
            "active",
            3,
        )
    migrations.run_migrations(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("select count(*) from session_turn_replies").fetchone() == (0,)


def controller_context(run_id: str):
    from modules.im import MessageContext

    return MessageContext(
        user_id="workbench",
        channel_id=SESSION,
        platform="avibe",
        platform_specific={
            "agent_session_id": SESSION,
            "turn_token": TURN,
            "task_trigger_kind": "scheduled",
            "task_execution_id": run_id,
        },
    )
