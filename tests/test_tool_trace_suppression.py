"""C-alt writer boundary: only proven redundant committed calls may omit a trace."""
from __future__ import annotations

from contextlib import closing
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import select

from core.agent_core.messages import ToolCallBlock, ToolResultMessage, text
from core.message_mirror import persist_agent_message
from core.tool_activity import source_link
from modules.im.base import MessageContext
from storage import migrations, tool_activity_service
from storage.compatibility import BRIDGE_REVISION
from storage.models import agent_events, agent_sessions, messages
from tests.agent_core.fakes import assistant
from tests.test_sqlite_migration_preflight import _snapshot
from tests.test_tool_activity_projection import _mark, _store
from tests.test_vibey_agent import SESSION, engine, session  # noqa: F401 -- isolated real-store fixtures


@pytest.mark.parametrize("case", [
    "supported", "unverified", "bridge-proof", "epoch-one", "unmarked", "unknown-version",
    "other-backend", "uncommitted", "malformed", "missing-response", "wrong-call", "wrong-session", "wrong-scope", "wrong-platform",
])
async def test_writer_requires_epoch_proof_and_exact_committed_response(engine, session, monkeypatch, case):
    store = await _store(engine)
    mark = _mark()
    if case == "unmarked":
        mark = {}
    elif case == "unknown-version":
        mark = _mark(version=99)
    elif case == "other-backend":
        mark = _mark(backend="codex")
    response = await store.append_response(SESSION, assistant("", calls=(ToolCallBlock("call", "read", {"path": "/work/a.py"}),)),
                                           final=False, metadata=mark)
    await store.append_tool_result(SESSION, ToolResultMessage("call", "read", (text("retained result"),)), details={})
    with engine.begin() as conn:
        # A Session's mutable current backend is never an adoption boundary.
        conn.execute(agent_sessions.update().where(agent_sessions.c.id == SESSION).values(agent_backend="claude", last_active_at="2020-01-01"))
        if case == "epoch-one":
            conn.exec_driver_sql("UPDATE state_meta SET value_json='1' WHERE key='compatibility_epoch'")
            conn.exec_driver_sql("UPDATE alembic_version SET version_num=?", (BRIDGE_REVISION,))
        if case == "uncommitted":
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(context_seq=None))
        elif case == "malformed":
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(content_json='{"model":{"message":[]}}'))
        elif case == "wrong-session":
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(session_id=None))
        elif case == "wrong-scope":
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(scope_id=None))
        elif case == "wrong-platform":
            conn.execute(messages.update().where(messages.c.id == response.row_id).values(platform="telegram"))
        before = [tuple(row) for row in conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_result"))]
        canonical = conn.execute(select(messages.c.content_json).where(messages.c.id == response.row_id)).scalar()
    monkeypatch.setattr("storage.compatibility.VERIFIED_PREDECESSOR_EPOCH", None if case == "unverified" else 1 if case == "bridge-proof" else 2)
    monkeypatch.setattr("core.message_mirror.get_cached_sqlite_engine", lambda: engine)
    context = MessageContext(user_id="fixture", channel_id="fixture", platform="avibe",
                             platform_specific={"agent_session_id": SESSION, "suppress_delivery": True})
    metadata = {"tool_call_source": source_link("missing" if case == "missing-response" else response.row_id,
                                               "wrong" if case == "wrong-call" else "call")}
    errors = []
    persist_agent_message(context, "toolcall", "rendered compatibility line", metadata=metadata, error_sink=errors)
    assert errors == []
    with engine.connect() as conn:
        traces = conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_call")).mappings().all()
        assert len(traces) == (0 if case == "supported" else 1)
        if traces:
            assert traces[0]["content_text"] == "rendered compatibility line"
            assert "text" not in json.loads(traces[0]["content_json"])
        assert [tuple(row) for row in conn.execute(select(agent_events).where(agent_events.c.event_type == "tool_result"))] == before
        assert conn.execute(select(messages.c.content_json).where(messages.c.id == response.row_id)).scalar() == canonical
        assert conn.execute(select(agent_sessions.c.last_active_at).where(agent_sessions.c.id == SESSION)).scalar() != "2020-01-01"
        if case == "supported":
            [step] = tool_activity_service.response_steps(conn, session_id=SESSION, response_ids=(response.row_id,))
            assert step["status"] == "completed" and step["started_at"] is None


@pytest.mark.no_sqlite_template
@pytest.mark.parametrize("crash", ["before-commit", "after-commit"])
def test_real_epoch_two_transition_is_atomic_and_keeps_fresh_rollback_snapshot(tmp_path, crash):
    path = tmp_path / "vibe.sqlite"
    migrations.run_migrations(path, revision=BRIDGE_REVISION)
    before = _snapshot(path)["database.sql"]
    decision = migrations.preflight_database(path)
    assert decision.compatibility_epoch == 1 and not decision.skip_snapshot
    result = subprocess.run([sys.executable, "-c", """
import os, sys
from pathlib import Path
from sqlalchemy import Engine, event
from storage import migrations
def crash_before_commit(conn, cursor, statement, parameters, context, executemany):
    if sys.argv[2] == 'before-commit' and statement.startswith('UPDATE alembic_version SET') and '20261108_0077' in statement:
        os._exit(71)
event.listen(Engine, 'after_cursor_execute', crash_before_commit)
migrations.run_migrations(Path(sys.argv[1]))
os._exit(72)
""", str(path), crash], check=False, capture_output=True, text=True)
    assert result.returncode == (71 if crash == "before-commit" else 72), result.stderr
    with closing(sqlite3.connect(path)) as conn:
        epoch = conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone()[0]
        stamp = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert (epoch, stamp) == (("1", BRIDGE_REVISION) if crash == "before-commit" else ("2", "20261108_0077"))
    [backup] = list((tmp_path / "backups").glob("*/vibe.sqlite"))
    assert _snapshot(backup)["database.sql"] == before
    if crash == "before-commit":
        assert _snapshot(path)["database.sql"] == before
    else:
        from alembic import command
        with pytest.raises(RuntimeError, match="restoring discards writes"):
            command.downgrade(migrations.alembic_config(path), BRIDGE_REVISION)


@pytest.mark.no_sqlite_template
def test_verified_epoch_two_keeps_no_copy_for_subsequent_additive_upgrades(tmp_path, monkeypatch):
    from storage import importer
    from tests.test_sqlite_readiness import COMPLETED, DATA_MARKER

    path = tmp_path / "vibe.sqlite"
    importer.ensure_sqlite_state(db_path=path, state_dir=path.parent)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO state_meta(key, value_json, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                     (DATA_MARKER, json.dumps(COMPLETED), COMPLETED["updated_at"]))
    graph = tmp_path / "alembic"
    shutil.copytree(migrations.alembic_dir(), graph, ignore=shutil.ignore_patterns("__pycache__"))
    (graph / "versions" / "20990101_9999_additive.py").write_text(
        "from alembic import op\nrevision='20990101_9999'\ndown_revision='20261108_0077'\n"
        "MIGRATION_COMPATIBILITY='additive'\n"
        "def upgrade():\n    op.create_index('ix_epoch_two_next', 'agents', ['description'], unique=False)\n"
    )
    monkeypatch.setattr(migrations, "alembic_dir", lambda: graph)
    decision = migrations.run_migrations(path)
    assert decision.compatibility_epoch == 2 and decision.skip_snapshot
    assert not list((tmp_path / "backups").glob("*/vibe.sqlite"))
