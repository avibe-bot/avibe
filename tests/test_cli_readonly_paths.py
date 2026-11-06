"""Current-state reads must not construct a writer or repair as a side effect.

Result-only CLI tests miss these writes. Exercise real handlers/stores and record
all rejected connection attempts, even when a best-effort handler catches one.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from config import paths
from core.scheduled_tasks import TaskExecutionStore
from core.show_pages import ShowPageStore
from core.show_session_events import ShowSessionEventStore
from core.vibe_agents import VibeAgentStore
from storage import importer, models
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from vibe import cli

pytestmark = pytest.mark.no_sqlite_template

_NOW = "2026-11-06T00:00:00+00:00"
_SESSION = "ses_readonly"
_AGENT = "readonly-agent"


@pytest.fixture
def current_state(sqlite_db_factory):
    db = sqlite_db_factory(paths.get_sqlite_state_path())
    engine = create_sqlite_engine(db)
    try:
        with engine.begin() as conn:
            conn.execute(models.state_meta.insert().values(
                key="migrations.legacy_deferred_resource_contexts.v1",
                value_json=json.dumps({"schema_version": 3, "state": "completed", "instance_id": "test"}),
                updated_at=_NOW,
            ))
            conn.execute(models.agents.insert().values(
                id="agent-readonly", name=_AGENT, normalized_name=_AGENT, backend="codex",
                model=None, enabled=1, source="custom", metadata_json="{}", created_at=_NOW, updated_at=_NOW,
            ))
            conn.execute(models.agent_sessions.insert().values(
                id=_SESSION, agent_id="agent-readonly", agent_name=_AGENT, agent_backend="codex",
                agent_variant="", session_anchor=_SESSION, native_session_id="native-readonly",
                title="Read-only fixture", status="active", metadata_json="{}",
                created_at=_NOW, updated_at=_NOW, last_active_at=_NOW,
            ))
            for kind, identifier in (("scheduled", "task-readonly"), ("watch", "watch-readonly")):
                conn.execute(models.run_definitions.insert().values(
                    id=identifier, definition_type=kind, name=identifier, session_id=_SESSION,
                    schedule_type="cron" if kind == "scheduled" else None,
                    cron="0 * * * *" if kind == "scheduled" else None, timezone="UTC",
                    command_json='["echo", "fixture"]', mode="once", enabled=1,
                    metadata_json="{}", created_at=_NOW, updated_at=_NOW,
                ))
            for identifier, status in (("run-readonly", "completed"), ("run-excluded", "failed")):
                conn.execute(models.agent_runs.insert().values(
                    id=identifier, run_type="agent_run", status=status, session_id=_SESSION,
                    agent_name=_AGENT, agent_backend="codex", metadata_json="{}",
                    created_at=_NOW, updated_at=_NOW, completed_at=_NOW,
                ))
            conn.execute(models.show_pages.insert().values(
                session_id=_SESSION, created_at=_NOW, updated_at=_NOW,
            ))
            # Metadata only: no key, ciphertext, custody or external effect.
            conn.execute(models.vault_secrets.insert().values(
                id="vault-readonly", name="READONLY_FIXTURE", tags='["fixture"]',
                created_at=_NOW, updated_at=_NOW,
            ))
    finally:
        engine.dispose()
    importer.reset_ensured_sqlite_state()
    assert importer.probe_current_sqlite_state(db)
    return db


@pytest.fixture
def only_read_connections(monkeypatch):
    connect = sqlite3.connect
    opened = []
    rejected = []

    def readonly_connect(database, *args, **kwargs):
        if "mode=ro" not in str(database):
            rejected.append(str(database))
            raise AssertionError("read command opened writable SQLite")
        opened.append(str(database))
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", readonly_connect)
    yield opened
    assert rejected == [], "a handler swallowed a write-capable connection attempt"


@pytest.mark.parametrize(("argv", "expected"), [
    (["agent", "list"], _AGENT),
    (["agent", "show", _AGENT], _AGENT),
    (["agent", "models", _AGENT], '"current"'),
    (["task", "list", "--global"], "task-readonly"),
    (["task", "show", "task-readonly"], "task-readonly"),
    (["watch", "list", "--global"], "watch-readonly"),
    (["watch", "show", "watch-readonly"], "watch-readonly"),
    (["runs", "list", "--global", "--status", "completed"], "run-readonly"),
    (["runs", "show", "run-readonly", "--brief"], "run-readonly"),
    (["harness", "status", "--global"], "watch-readonly"),
    (["session", "list"], _SESSION),
    (["session", "get", "--session-id", _SESSION], _SESSION),
    (["session", "queue", "list", "--session-id", _SESSION], _SESSION),
    (["vault", "list"], "READONLY_FIXTURE"),
    (["vault", "find", "READONLY"], "READONLY_FIXTURE"),
    (["vault", "tags"], "fixture"),
    (["show", "list", "--global"], _SESSION),
    (["show", "status", "--session-id", _SESSION, "--brief"], _SESSION),
    (["show", "status", "--session-id", _SESSION], _SESSION),
    (["show", "marks", "--session-id", _SESSION], _SESSION),
    (["data", "query", "--sql", "select name from agents"], _AGENT),
    (["data", "retention", "--json"], '"plan"'),
    (["data", "skill-usage"], '"enabled"'),
])
def test_current_cli_reads_never_open_writers(
    current_state, only_read_connections, monkeypatch, capsys, argv, expected,
):
    from vibe import api, internal_client

    async def unavailable_snapshot(**kwargs):
        return {"status_code": 503, "body": {"ok": False}}

    monkeypatch.setattr(internal_client, "list_running_agents", unavailable_snapshot)
    # The model catalog is an external dependency; the Agent row/ACL read stays real.
    monkeypatch.setattr(api, "agent_model_options", lambda *a, **k: {"ok": True, "models": []})
    monkeypatch.setattr("sys.argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as outcome:
        cli.main()
    captured = capsys.readouterr()
    assert outcome.value.code == 0, captured.err
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert expected in captured.out
    if argv[:2] == ["runs", "list"]:
        assert "run-excluded" not in captured.out
    assert only_read_connections
    with closing(sqlite3.connect(f"file:{current_state}?mode=ro", uri=True)) as conn:
        assert conn.execute("select model from agents where name=?", (_AGENT,)).fetchone() == (None,)


@pytest.mark.parametrize("factory", [VibeAgentStore, SQLiteBackgroundTaskStore, ShowPageStore, ShowSessionEventStore])
def test_readonly_store_enforces_query_only_without_prefill_or_index_repair(current_state, monkeypatch, factory):
    import storage.background as background

    def unexpected_repair(*args, **kwargs):
        pytest.fail("a readonly store attempted schema/index repair")

    monkeypatch.setattr(background, "ensure_background_indexes", unexpected_repair)
    store = factory(current_state, read_only=True)
    try:
        with store.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            assert conn.execute(select(models.agents.c.model)).scalar_one() is None
            # mode=ro alone allows temporary writes; query_only forbids them too.
            with pytest.raises(OperationalError, match="readonly"):
                conn.exec_driver_sql("CREATE TEMP TABLE forbidden_write (id INTEGER)")
    finally:
        store.close()


def test_writer_still_prefills_models_and_updates_rows(current_state):
    store = VibeAgentStore(current_state)
    try:
        assert store.require(_AGENT).model
        store.set_enabled(_AGENT, False)
        assert store.require(_AGENT).enabled is False
    finally:
        store.close()


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        (["agent", "list"], 0),
        (["task", "list", "--global"], 0),
        (["watch", "list", "--global"], 0),
        (["harness", "status", "--global"], 0),
        (["runs", "list", "--global"], 0),
        (["session", "list"], 0),
        (["vault", "list"], 0),
        (["show", "list", "--global"], 0),
        (["show", "marks", "--session-id", "missing-session"], 0),
        (["data", "retention", "--json"], 0),
        (["data", "skill-usage"], 0),
    ],
)
def test_cli_read_helpers_ensure_a_fresh_home(monkeypatch, capsys, argv, expected_code):
    monkeypatch.setattr("sys.argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as outcome:
        cli.main()
    captured = capsys.readouterr()
    assert "unable to open database file" not in captured.err.lower()
    assert outcome.value.code == expected_code
    assert paths.get_sqlite_state_path().exists()


@pytest.mark.parametrize("factory", [VibeAgentStore, SQLiteBackgroundTaskStore, ShowPageStore, ShowSessionEventStore])
def test_explicit_readonly_store_does_not_create_missing_state(tmp_path, factory):
    target = tmp_path / "missing" / "vibe.sqlite"
    store = None
    try:
        with pytest.raises(OperationalError):
            store = factory(target, read_only=True)
            with store.engine.connect():
                pass
    finally:
        if store is not None:
            store.close()
    assert not target.parent.exists()


def test_readonly_run_store_keeps_legacy_file_backend_explicit(tmp_path):
    with pytest.raises(ValueError, match="SQLite backend"):
        TaskExecutionStore(tmp_path / "legacy", read_only=True)
    assert not (tmp_path / "legacy").exists()


def test_binding_read_before_initialization_keeps_missing_result_without_creation():
    from storage.remote_access_authorization_service import load_instance_binding_state

    target = paths.get_sqlite_state_path()
    assert not target.exists()
    assert load_instance_binding_state(ensure=False) is None
    assert not target.exists()


def test_binding_read_existing_state_uses_a_readonly_connection(current_state, only_read_connections):
    from storage.remote_access_authorization_service import load_instance_binding_state

    assert load_instance_binding_state(ensure=False) is None
    assert only_read_connections


def test_read_command_still_initializes_a_missing_default_home(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["vibe", "agent", "list"])
    assert not paths.get_sqlite_state_path().exists()
    with pytest.raises(SystemExit) as outcome:
        cli.main()
    assert outcome.value.code == 0
    assert json.loads(capsys.readouterr().out)["agents"] == []
    assert paths.get_sqlite_state_path().exists()
