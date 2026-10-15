"""Bounded Vibey T3 recovery over the real Message/Delivery boundary.

The regression this owns is the second-head finding in PR #2689: accepted hidden
Vibey retry Deliveries remain null-context forever, so a Python-side filter made
every recovery pass scan retry history and hydrate attachments/Deliveries before
throwing those rows away.  The owning query must exclude the trusted control
shape and use the partial index, while ordinary and forged-marker inputs remain
recoverable.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest
from alembic import command
from sqlalchemy import event

from modules.agents.vibey import VibeyAgent
from storage import message_deliveries
from storage.db import create_sqlite_engine
from storage.migrations import alembic_config, run_migrations
from storage.models import agent_sessions
from storage.vibey_recovery import VIBEY_RECOVERY_INPUT_PREDICATE

pytestmark = pytest.mark.no_sqlite_template

_NOW = "2026-10-16T00:00:00.000000Z"
_SESSION = "ses_recovery"
_MIGRATION = importlib.import_module(
    "storage.alembic.versions.20261016_0067_vibey_recovery_input_index"
)


def _insert_session(conn, session_id: str) -> None:
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            agent_backend="vibey",
            agent_variant="vibey",
            session_anchor=session_id,
            workdir="/tmp",
            native_session_id=session_id,
            status="active",
            visibility="foreground",
            pinned=0,
            agent_status="idle",
            metadata_json="{}",
            created_at=_NOW,
            updated_at=_NOW,
            last_active_at=_NOW,
        )
    )


def _accepted_input(
    engine,
    session_id: str,
    ordinal: int,
    *,
    author: str,
    source: str,
    message_type: str,
    text: str | None,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    delivery_id = f"dlv_recovery_{session_id}_{ordinal}"
    turn_id = f"turn_recovery_{session_id}_{ordinal}"
    dispatch_text = text or ""
    with engine.begin() as conn:
        delivery = message_deliveries.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id=session_id,
            priority="p3",
            state="reserved",
            snapshot=message_deliveries.message_snapshot(
                scope_id=None,
                session_id=session_id,
                platform="avibe",
                author=author,
                source=source,
                message_type=message_type,
                text=text,
                metadata=metadata,
            ),
            dispatch_text=dispatch_text,
            now=_NOW,
        )
        claimed = message_deliveries.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id=session_id,
            backend="vibey",
            deliveries=[delivery],
            dispatch_text=dispatch_text,
        )
        started = message_deliveries.bind_native_start(
            conn,
            turn_id=turn_id,
            expected_version=int(claimed["turn"]["version"]),
            runtime_key="test",
            runtime_turn_id=turn_id,
            native_turn_id=f"vibey:test:{turn_id}",
        )
        assert started is not None
        message_deliveries.materialize_start_acceptance(
            conn,
            turn_id=turn_id,
            evidence={"kind": "test"},
        )
        message_deliveries.terminalize_turn(
            conn,
            turn_id,
            outcome="completed",
            settled_by="test",
            evidence_kind="test",
        )
    return delivery_id, turn_id


@pytest.fixture
def recovery_engine(tmp_path: Path):
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    with engine.begin() as conn:
        _insert_session(conn, _SESSION)
    try:
        yield engine
    finally:
        engine.dispose()


def _agent(engine) -> VibeyAgent:
    agent = object.__new__(VibeyAgent)
    agent._engine = engine
    return agent


def _recovery_query_plan(
    engine,
    operation: Callable[[], Any],
) -> str:
    statements: list[tuple[str, Any]] = []

    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "message_deliveries" in statement.lower() and "context_seq is null" in statement.lower():
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        operation()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements, "the recovery operation did not issue its Message query"
    statement, parameters = statements[-1]
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            f"EXPLAIN QUERY PLAN {statement}", parameters
        ).all()
    return " ".join(str(row[3]) for row in rows)


def test_migration_round_trip_owns_the_recovery_index(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision=_MIGRATION.down_revision)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select name from sqlite_master where type = 'index' and name = ?",
            (_MIGRATION._INDEX,),
        ).fetchone() is None
        assert conn.execute(
            "select name from sqlite_master where type = 'index' and name = ?", (_MIGRATION._DELIVERY_INDEX,)
        ).fetchone() is None

    run_migrations(db_path)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?",
            (_MIGRATION._INDEX,),
        ).fetchone()
        assert row is not None
        assert row[0].lower() == _MIGRATION.CREATE_INDEX_SQL.lower()
        assert [item[2] for item in conn.execute(f"pragma index_info({_MIGRATION._INDEX})")] == [
            "context_seq",
            "session_id",
            "id",
        ]
        assert [item[2] for item in conn.execute(f"pragma index_info({_MIGRATION._DELIVERY_INDEX})")] == ["message_id"]

    command.downgrade(alembic_config(db_path), _MIGRATION.down_revision)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select name from sqlite_master where type = 'index' and name = ?",
            (_MIGRATION._INDEX,),
        ).fetchone() is None
        assert conn.execute(
            "select name from sqlite_master where type = 'index' and name = ?", (_MIGRATION._DELIVERY_INDEX,)
        ).fetchone() is None


def test_recovery_excludes_hidden_history_before_delivery_and_attachment_hydration(
    recovery_engine, monkeypatch
) -> None:
    hidden_ids = {
        _accepted_input(
            recovery_engine,
            _SESSION,
            ordinal,
            author="harness",
            source="harness",
            message_type="agent_initiated",
            text=None,
            metadata={"vibey_resume": True, "retry_notice_id": f"notice-{ordinal}"},
        )[0]
        for ordinal in range(300)
    }
    real_id, _ = _accepted_input(
        recovery_engine,
        _SESSION,
        1000,
        author="harness",
        source="harness",
        message_type="agent_initiated",
        text="continue the real agent-initiated input",
        metadata={"origin": "watch"},
    )
    forged_id, _ = _accepted_input(
        recovery_engine,
        _SESSION,
        1001,
        author="user",
        source="user",
        message_type="user",
        text="a user message carrying a forged marker",
        metadata={"vibey_resume": True},
    )
    missing = object()
    negative_ids = []
    for ordinal, marker in enumerate((1, 1.0, "true", None, False, missing), start=1100):
        metadata = {} if marker is missing else {"vibey_resume": marker}
        negative_ids.append(
            _accepted_input(
                recovery_engine,
                _SESSION,
                ordinal,
                author="harness",
                source="harness",
                message_type="agent_initiated",
                text=f"marker negative {ordinal}",
                metadata=metadata,
            )[0]
        )
    null_source_id, _ = _accepted_input(
        recovery_engine,
        _SESSION,
        1200,
        author="harness",
        source=None,
        message_type="agent_initiated",
        text="null source is an ordinary input",
        metadata={"vibey_resume": True},
    )

    delivery_lookups: list[str] = []
    attachment_lookups: list[tuple[str, list[Any]]] = []
    original_get_delivery = message_deliveries.get_delivery
    monkeypatch.setattr(
        message_deliveries,
        "get_delivery",
        lambda conn, delivery_id: (
            delivery_lookups.append(str(delivery_id))
            or original_get_delivery(conn, delivery_id)
        ),
    )
    import core.workbench_media as workbench_media

    original_resolve = workbench_media.resolve_attachment_specs
    monkeypatch.setattr(
        workbench_media,
        "resolve_attachment_specs",
        lambda conn, *, session_id, attachments: (
            attachment_lookups.append((str(session_id), list(attachments)))
            or original_resolve(conn, session_id=session_id, attachments=attachments)
        ),
    )

    agent = _agent(recovery_engine)
    inputs = agent._unconsumed_inputs(_SESSION)

    assert {row[0] for row in inputs} == {real_id, forged_id, *negative_ids, null_source_id}
    assert hidden_ids.isdisjoint(delivery_lookups)
    assert set(delivery_lookups) == {real_id, forged_id, *negative_ids, null_source_id}
    assert len(attachment_lookups) == 9
    delivery_lookups.clear()
    assert agent._sessions_with_unconsumed_inputs() == [_SESSION]
    assert delivery_lookups == []


def test_recovery_queries_seek_the_partial_index(recovery_engine) -> None:
    _accepted_input(
        recovery_engine,
        _SESSION,
        1,
        author="harness",
        source="harness",
        message_type="agent_initiated",
        text=None,
        metadata={"vibey_resume": True},
    )
    _accepted_input(
        recovery_engine,
        _SESSION,
        2,
        author="user",
        source="user",
        message_type="user",
        text="real input",
    )
    agent = _agent(recovery_engine)

    session_plan = _recovery_query_plan(
        recovery_engine,
        lambda: agent._unconsumed_inputs(_SESSION),
    )
    global_plan = _recovery_query_plan(
        recovery_engine,
        agent._sessions_with_unconsumed_inputs,
    )

    for plan in (session_plan, global_plan):
        assert "USING INDEX ix_messages_vibey_recovery_input" in plan, (
            f"session_plan={session_plan}; global_plan={global_plan}"
        )
        assert "USING INDEX ix_message_deliveries_message (message_id=?)" in plan, plan
        for table in ("messages", "message_deliveries", "session_turns"):
            assert f"SCAN {table}" not in plan, plan
            assert f"SEARCH {table}" in plan, plan
    assert "context_seq=? AND session_id=?" in session_plan
    assert "context_seq=?" in global_plan
    assert VIBEY_RECOVERY_INPUT_PREDICATE in _MIGRATION.CREATE_INDEX_SQL
