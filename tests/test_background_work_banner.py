"""Unified background-work banner — union derivation.

Covers the presentation-layer union assembled at runtime-state build time
(docs/plans/unified-background-work-banner.md): backend activities from the
process-local ``SessionActivityRegistry`` joined with harness items
(watches / scheduled tasks / delegated agent runs) derived LIVE from the durable
store. The harness items must derive from the DB, never from the registry, so the
banner stays correct across a restart.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import insert

from core.session_activities import SessionActivityRegistry
from core.session_turns import SessionTurnManager
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_runs, run_definitions
from storage.workbench_sessions_service import derive_session_harness_activities

_NOW = "2026-07-16T00:00:00Z"


def _make_engine(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    return create_sqlite_engine(db_path), db_path


def _insert_definition(engine, **overrides) -> str:
    values = {
        "id": overrides.pop("id"),
        "definition_type": overrides.pop("definition_type"),
        "name": None,
        "session_id": None,
        "prompt": None,
        "message": None,
        "enabled": 1,
        "deleted_at": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "metadata_json": "{}",
    }
    values.update(overrides)
    with engine.begin() as conn:
        conn.execute(insert(run_definitions).values(**values))
    return values["id"]


def _insert_run(engine, **overrides) -> str:
    values = {
        "id": overrides.pop("id"),
        "run_type": "agent_run",
        "status": "running",
        "agent_name": None,
        "agent_backend": None,
        "message": None,
        "prompt": None,
        "session_id": None,
        "callback_session_id": None,
        # Async delegated runs carry a pending callback while running; the banner
        # only surfaces these (sync --sync runs leave callback_status null).
        "callback_status": "pending",
        "created_at": _NOW,
        "started_at": _NOW,
        "updated_at": _NOW,
        "metadata_json": "{}",
    }
    values.update(overrides)
    with engine.begin() as conn:
        conn.execute(insert(agent_runs).values(**values))
    return values["id"]


def test_each_harness_source_contributes_one_row(tmp_path: Path):
    engine, _ = _make_engine(tmp_path)
    _insert_definition(
        engine,
        id="watch-1",
        definition_type="watch",
        name="deploy watch",
        session_id="ses-1",
    )
    _insert_definition(
        engine,
        id="task-1",
        definition_type="scheduled",
        name="nightly report",
        session_id="ses-1",
        schedule_type="cron",
    )
    _insert_run(
        engine,
        id="run-1",
        agent_name="worker",
        message="audit the contract",
        session_id="ses-delegate",
        callback_session_id="ses-1",
    )

    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")

    by_kind = {item["item_kind"]: item for item in items}
    assert set(by_kind) == {"watch", "task", "agent_run"}
    assert by_kind["watch"]["id"] == "watch:watch-1"
    assert by_kind["watch"]["label"] == "deploy watch"
    assert by_kind["watch"]["schedule_type"] is None
    assert by_kind["task"]["id"] == "task:task-1"
    assert by_kind["task"]["label"] == "nightly report"
    assert by_kind["task"]["schedule_type"] == "cron"
    assert by_kind["agent_run"]["id"] == "agent_run:run-1"
    assert by_kind["agent_run"]["label"] == "worker: audit the contract"
    assert by_kind["agent_run"]["schedule_type"] is None
    # ``since`` and a stable id are present on every unified item.
    assert all(item["since"] == _NOW for item in items)
    assert all(item["id"] for item in items)


def test_task_schedule_type_comes_from_the_durable_definition(tmp_path: Path):
    engine, _ = _make_engine(tmp_path)
    _insert_definition(
        engine,
        id="task-once",
        definition_type="scheduled",
        name="title says every day but is one-shot",
        session_id="ses-1",
        schedule_type="at",
    )
    _insert_definition(
        engine,
        id="task-recurring",
        definition_type="scheduled",
        name="title says one-off but is recurring",
        session_id="ses-1",
        schedule_type="cron",
    )

    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")

    assert {item["id"]: item["schedule_type"] for item in items} == {
        "task:task-once": "at",
        "task:task-recurring": "cron",
    }


def test_task_label_falls_back_to_prompt_when_unnamed(tmp_path: Path):
    engine, _ = _make_engine(tmp_path)
    _insert_definition(
        engine,
        id="task-2",
        definition_type="scheduled",
        name=None,
        prompt="summarize the daily standup notes",
        session_id="ses-1",
    )
    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")
    assert len(items) == 1
    assert items[0]["label"] == "summarize the daily standup notes"


def test_excludes_disabled_deleted_and_foreign_and_terminal(tmp_path: Path):
    engine, _ = _make_engine(tmp_path)
    # Disabled watch: paused, not ongoing background work.
    _insert_definition(
        engine, id="w-off", definition_type="watch", session_id="ses-1", enabled=0
    )
    # Soft-deleted task.
    _insert_definition(
        engine,
        id="t-del",
        definition_type="scheduled",
        session_id="ses-1",
        deleted_at=_NOW,
    )
    # Watch bound to a different session.
    _insert_definition(
        engine, id="w-other", definition_type="watch", session_id="ses-2"
    )
    # Delegated run whose callback returns to a different session.
    _insert_run(
        engine, id="r-other", callback_session_id="ses-2", session_id="ses-x"
    )
    # Terminal delegated run (already finished) — not pending work.
    _insert_run(
        engine,
        id="r-done",
        status="succeeded",
        callback_session_id="ses-1",
        session_id="ses-x",
    )
    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")
    assert items == []


def test_self_run_is_not_counted_as_dispatched_work(tmp_path: Path):
    # A run whose callback target equals its execution session is the session's
    # own turn (reported via ``foreground``), not delegated background work.
    engine, _ = _make_engine(tmp_path)
    _insert_run(
        engine, id="r-self", callback_session_id="ses-1", session_id="ses-1"
    )
    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")
    assert items == []


def test_sync_run_without_pending_callback_is_excluded(tmp_path: Path):
    # A synchronous ``--sync`` delegated run carries callback_session_id but a
    # null callback_status (caller waits inline; no callback owed), so it is NOT
    # background work this session is waiting on.
    engine, _ = _make_engine(tmp_path)
    _insert_run(
        engine,
        id="r-sync",
        callback_session_id="ses-1",
        session_id="ses-delegate",
        callback_status=None,
    )
    with engine.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")
    assert items == []


def test_items_derive_from_db_so_banner_survives_restart(tmp_path: Path):
    # The registry is process-local and empty after a restart; harness items must
    # come from the durable store. Simulate a restart by disposing the engine and
    # opening a fresh one on the same DB file — the item must still derive.
    engine, db_path = _make_engine(tmp_path)
    _insert_definition(
        engine, id="watch-r", definition_type="watch", name="w", session_id="ses-1"
    )
    engine.dispose()

    reopened = create_sqlite_engine(db_path)
    with reopened.connect() as conn:
        items = derive_session_harness_activities(conn, "ses-1")
    assert [item["id"] for item in items] == ["watch:watch-r"]
    assert items[0]["item_kind"] == "watch"


def test_turn_state_unions_backend_activities_and_harness_items(tmp_path: Path):
    # End-to-end at the assembly point: registry backend activity + DB-derived
    # harness items land in one ``background_activities`` list, tagged by kind.
    engine, _ = _make_engine(tmp_path)
    _insert_definition(
        engine, id="watch-u", definition_type="watch", name="w", session_id="ses-1"
    )
    _insert_run(
        engine,
        id="run-u",
        agent_name="worker",
        message="go",
        session_id="ses-delegate",
        callback_session_id="ses-1",
    )

    registry = SessionActivityRegistry()
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="bg-1",
        kind="background_task",
        description="Running checks",
    )
    manager = SessionTurnManager(
        controller=SimpleNamespace(agent_service=SimpleNamespace(activities=registry))
    )
    manager._engine = engine

    state = manager.turn_state("ses-1")
    activities = state["background_activities"]
    kinds = [item["item_kind"] for item in activities]
    assert kinds.count("backend_activity") == 1
    assert kinds.count("watch") == 1
    assert kinds.count("agent_run") == 1
    backend_item = next(i for i in activities if i["item_kind"] == "backend_activity")
    assert backend_item["id"] == "bg-1"
    assert backend_item["label"] == "Running checks"
    assert backend_item["since"] == backend_item["started_at"]


def test_turn_state_survives_harness_derivation_failure(tmp_path: Path):
    # A harness query failure must degrade to registry-only activities, never
    # break the turn-state payload.
    registry = SessionActivityRegistry()
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="bg-1",
        kind="background_task",
    )
    manager = SessionTurnManager(
        controller=SimpleNamespace(agent_service=SimpleNamespace(activities=registry))
    )
    # A bare object() as the connection makes derive_session_harness_activities
    # raise inside turn_state's guarded block.
    manager._engine = SimpleNamespace(begin=lambda: nullcontext(object()))

    import core.session_turns as session_turns

    original = session_turns.messages_service.list_queued
    session_turns.messages_service.list_queued = lambda conn, sid: []
    try:
        state = manager.turn_state("ses-1")
    finally:
        session_turns.messages_service.list_queued = original

    assert [item["id"] for item in state["background_activities"]] == ["bg-1"]
    assert state["background_activities"][0]["item_kind"] == "backend_activity"


def test_banner_enabled_pref_round_trip(tmp_path: Path):
    # Global toggle (spec req 2): default ON, persisted in state_meta, hermetic
    # via explicit db_path (never touches real state).
    from core.workbench_prefs import (
        get_background_work_banner_enabled,
        set_background_work_banner_enabled,
    )

    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")

    assert get_background_work_banner_enabled(db_path=db_path) is True  # default ON
    assert set_background_work_banner_enabled(False, db_path=db_path) is False
    assert get_background_work_banner_enabled(db_path=db_path) is False
    assert set_background_work_banner_enabled(True, db_path=db_path) is True
    assert get_background_work_banner_enabled(db_path=db_path) is True


def test_definition_session_filter_scopes_watches_and_tasks(tmp_path: Path):
    # Spec req 4: the Harness "只看本会话" chip filters definitions by bound
    # session. Rows for session A must exclude session B's definitions.
    engine, db_path = _make_engine(tmp_path)
    _insert_definition(engine, id="w-a", definition_type="watch", name="wa", session_id="ses-A")
    _insert_definition(engine, id="w-b", definition_type="watch", name="wb", session_id="ses-B")
    _insert_definition(engine, id="t-a", definition_type="scheduled", name="ta", session_id="ses-A")
    _insert_definition(engine, id="t-b", definition_type="scheduled", name="tb", session_id="ses-B")

    store = SQLiteBackgroundTaskStore(db_path=db_path)
    try:
        watches_a = store.list_watches_page(session_id="ses-A", page_request=None)
        assert [w["id"] for w in watches_a.items] == ["w-a"]
        tasks_a = store.list_scheduled_tasks_page(session_id="ses-A", page_request=None)
        assert [t["id"] for t in tasks_a.items] == ["t-a"]
        assert store.count_watches(session_id="ses-A")["all"] == 1
        assert store.count_scheduled_tasks(session_id="ses-B")["all"] == 1
        # No filter → both sessions' definitions are returned.
        assert len(store.list_watches_page(page_request=None).items) == 2
    finally:
        store.close()
