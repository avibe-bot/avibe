"""Settling a Model Hub turn stamps its mode on that turn's Message rows by index, off the event loop.

The stamp used to scan every Message (``SCAN messages``) on the event loop while holding the
database write lock; on a long-running host that is hundreds of megabytes per settled turn.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import closing
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select

from core.handlers.model_hub.service import ModelHubService
from modules.agents import model_hub
from storage.db import create_sqlite_engine
from storage.migrations import _normalized_index_sql, run_migrations
from storage.models import agent_sessions, messages, metadata

pytestmark = pytest.mark.no_sqlite_template

_REVISION = import_module("storage.alembic.versions.20261012_0064_messages_turn_id_index")
_NOW = "2026-10-12T00:00:00.000000Z"


def _seed(engine, rows: dict[str, str]) -> None:
    """One session, and one agent Message per ``id -> metadata_json``."""

    with engine.begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id="ses_stamp",
                agent_backend="codex",
                agent_variant="codex",
                session_anchor="anchor",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        for message_id, metadata_json in rows.items():
            conn.execute(
                messages.insert().values(
                    id=message_id,
                    session_id="ses_stamp",
                    platform="avibe",
                    author="agent",
                    type="result",
                    source="agent",
                    content_json="{}",
                    metadata_json=metadata_json,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )


@pytest.fixture
def migrated_engine(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    engine = create_sqlite_engine(db_path)
    monkeypatch.setattr("core.handlers.model_hub.service.get_cached_sqlite_engine", lambda: engine)
    yield engine
    engine.dispose()


def _index_sql(db_path: Path) -> str | None:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "select sql from sqlite_master where type = 'index' and name = ?", (_REVISION._INDEX,)
        ).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(lambda: ModelHubService.note_turn_mode("turn_a", "hub"), id="mode-stamp"),
        pytest.param(lambda: ModelHubService._known_turn("turn_a"), id="provenance-read"),
    ],
)
def test_the_turn_is_found_by_the_turn_id_index_instead_of_a_scan(migrated_engine, operation) -> None:
    statements: list[tuple[str, tuple]] = []

    @event.listens_for(migrated_engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, parameters, _context, _executemany):
        if "messages" in statement:
            statements.append((statement, tuple(parameters)))

    operation()

    [(statement, parameters)] = statements
    with migrated_engine.connect() as conn:
        plan = " ".join(
            str(row[3]) for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters)
        )
    assert f"USING INDEX {_REVISION._INDEX}" in plan
    assert "SCAN messages" not in plan


def test_the_stamp_lands_on_every_row_of_its_turn_and_on_no_other(migrated_engine) -> None:
    _seed(
        migrated_engine,
        {
            "msg_result": json.dumps({"turn_id": "turn_a"}),
            "msg_output": json.dumps({"turn_id": "turn_a", "kind": "output"}),
            "msg_other_turn": json.dumps({"turn_id": "turn_b"}),
            "msg_no_turn": json.dumps({"source": "agent"}),
            # The index guards json_valid, so an unparseable row stays writable and is skipped.
            "msg_malformed": "{not json",
        },
    )

    ModelHubService.note_turn_mode("turn_a", "hub")

    with migrated_engine.connect() as conn:
        stored = dict(conn.execute(select(messages.c.id, messages.c.metadata_json)).all())
    assert json.loads(stored["msg_result"]) == {"turn_id": "turn_a", "model_hub_mode": "hub"}
    assert json.loads(stored["msg_output"]) == {"turn_id": "turn_a", "kind": "output", "model_hub_mode": "hub"}
    assert json.loads(stored["msg_other_turn"]) == {"turn_id": "turn_b"}
    assert json.loads(stored["msg_no_turn"]) == {"source": "agent"}
    assert stored["msg_malformed"] == "{not json"


def test_turn_provenance_reads_the_stamped_mode_by_turn(migrated_engine) -> None:
    _seed(
        migrated_engine,
        {
            "msg_result": json.dumps({"turn_id": "turn_a", "model_hub_mode": "hub"}),
            "msg_other_turn": json.dumps({"turn_id": "turn_b", "model_hub_mode": "direct"}),
            "msg_malformed": "{not json",
        },
    )

    assert ModelHubService._known_turn("turn_a") == ("codex", "hub")
    assert ModelHubService._known_turn("turn_b") == ("codex", "direct")
    assert ModelHubService._known_turn("turn_missing") == (None, None)


def test_the_migration_indexes_a_released_database_and_downgrades_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path, revision=_REVISION.down_revision)
    engine = create_sqlite_engine(db_path)
    try:
        _seed(engine, {"msg_released": json.dumps({"turn_id": "turn_released"}), "msg_malformed": "{not json"})
    finally:
        engine.dispose()
    assert _index_sql(db_path) is None

    run_migrations(db_path)

    assert _normalized_index_sql(_index_sql(db_path)) == _normalized_index_sql(_REVISION.CREATE_INDEX_SQL)
    with closing(sqlite3.connect(db_path)) as conn:
        found = conn.execute(
            f"select id from messages where {_REVISION._TURN_ID_EXPR} = ?", ("turn_released",)
        ).fetchall()
        assert found == [("msg_released",)]

    from alembic import command
    from storage import migrations

    command.downgrade(migrations.alembic_config(db_path), _REVISION.down_revision)
    assert _index_sql(db_path) is None


def test_a_head_shaped_database_gets_the_index_from_its_revision(tmp_path: Path) -> None:
    # A database born from ``metadata.create_all`` reaches head through the repair path.
    db_path = tmp_path / "vibe.sqlite"
    engine = create_sqlite_engine(db_path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()

    run_migrations(db_path)

    assert _normalized_index_sql(_index_sql(db_path)) == _normalized_index_sql(_REVISION.CREATE_INDEX_SQL)


def test_the_query_expression_is_the_indexed_one() -> None:
    from storage.models import MESSAGE_TURN_ID_SQL

    assert MESSAGE_TURN_ID_SQL == _REVISION._TURN_ID_EXPR


def test_settling_a_turn_does_not_wait_for_its_mode_stamp() -> None:
    stamping = threading.Event()
    release = threading.Event()
    stamped: list[tuple[str, str]] = []

    def note_turn_mode(turn_id, mode):
        stamping.set()
        assert release.wait(10)
        stamped.append((turn_id, mode))

    correlation = Mock()
    router = model_hub.ModelHubRuntimeRouter(
        service=SimpleNamespace(note_turn_mode=note_turn_mode),
        turn_gateway=SimpleNamespace(
            finalize_turn=lambda turn_id, *, settled_by, finish: finish(),
            correlation=correlation,
        ),
        native_cli_ready=lambda backend: False,
    )

    router.settle_turn("turn_a", settled_by="terminal_result", ts=_NOW, mode="hub")

    # Settlement finished while the stamp was still blocked on its own thread.
    correlation.settle.assert_called_once_with("turn_a", settled_by="terminal_result", ts=_NOW)
    assert stamping.wait(10)
    assert stamped == []
    release.set()
    model_hub._TURN_MODE_WRITER.submit(lambda: None).result(timeout=10)
    assert stamped == [("turn_a", "hub")]


def test_a_failed_mode_stamp_is_logged_and_never_fails_settlement(caplog) -> None:
    correlation = Mock()
    router = model_hub.ModelHubRuntimeRouter(
        service=SimpleNamespace(note_turn_mode=Mock(side_effect=OSError("mode store unavailable"))),
        turn_gateway=SimpleNamespace(
            finalize_turn=lambda turn_id, *, settled_by, finish: finish(),
            correlation=correlation,
        ),
        native_cli_ready=lambda backend: False,
    )

    router.settle_turn("turn_a", settled_by="terminal_result", ts=_NOW, mode="hub")
    model_hub._TURN_MODE_WRITER.submit(lambda: None).result(timeout=10)

    correlation.settle.assert_called_once()
    assert "Model Hub turn mode was not stored" in caplog.text


def test_shutdown_waits_for_queued_stamps_only_up_to_its_bound_and_discards_the_rest(caplog) -> None:
    release = threading.Event()
    ran: list[int] = []
    model_hub._TURN_MODE_WRITER.submit(release.wait, 10)
    queued = [model_hub._TURN_MODE_WRITER.submit(ran.append, index) for index in range(3)]

    started = time.monotonic()
    assert model_hub.drain_turn_mode_stamps(timeout=0.2) is False
    assert time.monotonic() - started < 1.0
    assert all(stamp.cancelled() for stamp in queued)
    assert "3 discarded" in caplog.text

    release.set()
    assert model_hub.drain_turn_mode_stamps(timeout=10) is True
    assert ran == []


def test_a_futures_timeout_from_the_drain_is_caught_on_every_python(monkeypatch) -> None:
    # Python 3.10 raises concurrent.futures.TimeoutError, which is not the builtin there.
    class TimingOut(Future):
        def result(self, timeout=None):
            raise FuturesTimeoutError()

    monkeypatch.setattr(model_hub._TURN_MODE_WRITER, "submit", lambda fn, *args: TimingOut())

    assert model_hub.drain_turn_mode_stamps(timeout=0.1) is False


# A service process whose stamp writer is blocked on a write that will not finish, with more queued.
_EXIT_WITH_A_BLOCKED_STAMP = """
import sys, time
sys.path.insert(0, sys.argv[1])
from modules.agents import model_hub
marker = sys.argv[2]
model_hub._TURN_MODE_WRITER.submit(time.sleep, 60)
for _ in range(3):
    model_hub._TURN_MODE_WRITER.submit(lambda: open(marker, "a").write("ran"))
drained = model_hub.drain_turn_mode_stamps(timeout=0.5)
print(drained, time.time(), flush=True)
"""


def test_a_blocked_stamp_never_holds_process_exit(tmp_path: Path) -> None:
    marker = tmp_path / "queued-stamp-ran"
    finished = subprocess.run(
        [sys.executable, "-c", _EXIT_WITH_A_BLOCKED_STAMP, str(Path(__file__).resolve().parents[1]), str(marker)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    exited_at = time.time()

    assert finished.returncode == 0, finished.stderr
    drained, drained_at = finished.stdout.split()
    assert drained == "False"
    # From the drain's return to the process being gone, despite the stamp still sleeping.
    assert exited_at - float(drained_at) < 3.0
    assert not marker.exists()
