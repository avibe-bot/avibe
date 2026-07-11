from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.session_activities import SessionActivityRegistry
from core.session_turns import SessionTurnManager
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.session_activities import SQLiteSessionActivityStore


def test_activity_lifecycle_keeps_state_axes_orthogonal():
    registry = SessionActivityRegistry()

    registry.set_connection(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        state="connected",
    )
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
        description="Run checks",
    )

    state = registry.session_state("ses-1")
    assert state["connection"] == "connected"
    assert [item["id"] for item in state["background_activities"]] == ["task-1"]

    completed = registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-1",
        status="completed",
        expects_output=True,
    )
    assert completed is not None
    assert registry.session_state("ses-1") == {
        "background_activities": [],
        "pending_activity_output_count": 1,
        "connection": "connected",
    }

    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None
    assert claimed.id == "task-1"
    assert registry.claim_completed_output("claude", "runtime-1") is None
    registry.ack_completed_output(claimed)
    assert registry.has_completed_output("claude", "runtime-1") is False


def test_activity_updates_are_independent_and_runtime_disconnect_terminates_all():
    registry = SessionActivityRegistry()
    for task_id in ("task-1", "task-2"):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=task_id,
            kind="background_task",
        )

    registry.progress(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-2",
        description="Still running",
        metadata={"last_tool_name": "Bash"},
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-1",
        status="failed",
    )

    active = registry.active_for_runtime("claude", "runtime-1")
    assert [item.id for item in active] == ["task-2"]
    assert active[0].metadata["last_tool_name"] == "Bash"

    completed = registry.end_runtime("claude", "runtime-1", status="disconnected")
    assert registry.active_for_runtime("claude", "runtime-1") == []
    assert registry.session_state("ses-1")["connection"] == "disconnected"
    assert [(item.id, item.status) for item in completed] == [
        ("task-2", "disconnected"),
    ]


def test_runtime_disconnect_preserves_completed_output_until_delivery():
    registry = SessionActivityRegistry()
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-1",
        status="completed",
        metadata={"summary": "Background work finished"},
        expects_output=True,
    )

    registry.end_runtime("claude", "runtime-1", status="disconnected")

    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None
    assert claimed.id == "task-1"
    assert claimed.metadata["summary"] == "Background work finished"


def test_turn_state_composes_foreground_inbox_activity_and_connection_axes():
    registry = SessionActivityRegistry()
    registry.set_connection(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        state="connected",
    )
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
    )
    manager = SessionTurnManager(
        controller=SimpleNamespace(
            agent_service=SimpleNamespace(activities=registry),
        )
    )
    manager._engine = SimpleNamespace(begin=lambda: nullcontext(object()))

    with mock.patch(
        "core.session_turns.messages_service.list_queued",
        return_value=[{"id": "queued-1"}],
    ):
        state = manager.turn_state("ses-1")

    assert state["in_flight"] is False
    assert state["foreground"] == "idle"
    assert state["pending_input_count"] == 1
    assert state["connection"] == "connected"
    assert [item["id"] for item in state["background_activities"]] == ["task-1"]


def test_activity_restart_recovers_connection_and_interrupts_live_work(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    first = SessionActivityRegistry(store)
    first.set_connection(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        state="connected",
    )
    first.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-live",
        kind="background_task",
        run_id="run-1",
    )

    recovered = SessionActivityRegistry(store)

    assert recovered.active_for_runtime("claude", "runtime-1") == []
    assert recovered.session_state("ses-1")["connection"] == "disconnected"
    terminals = recovered.drain_recovered_terminals()
    assert [(item.id, item.status, item.run_id) for item in terminals] == [
        ("task-live", "disconnected", "run-1"),
    ]
    assert store.list_activities() == []
    engine.dispose()


def test_completed_activity_output_is_durable_until_ack(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    first = SessionActivityRegistry(store)
    first.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-complete",
        kind="background_task",
        run_id="run-1",
    )
    first.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-complete",
        status="completed",
        metadata={"summary": "Recovered summary"},
        expects_output=True,
    )

    recovered = SessionActivityRegistry(store)
    claimed = recovered.claim_completed_output(
        "claude",
        "runtime-1",
        recovered_only=True,
    )

    assert claimed is not None
    assert claimed.metadata["summary"] == "Recovered summary"
    assert recovered.has_pending_run_output("run-1") is True
    assert len(store.list_activities()) == 1

    recovered.ack_completed_output(claimed)
    assert recovered.has_pending_run_output("run-1") is False
    assert store.list_activities() == []
    engine.dispose()


def test_activity_restart_persists_inferred_disconnected_connection(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    first = SessionActivityRegistry(store)
    first.start(
        backend="claude",
        runtime_key="runtime-without-connection",
        session_id="ses-1",
        activity_id="task-live",
        kind="background_task",
    )

    SessionActivityRegistry(store)

    assert store.list_connections() == [
        {
            "version": 1,
            "backend": "claude",
            "runtime_key": "runtime-without-connection",
            "session_id": "ses-1",
            "state": "disconnected",
        }
    ]
    engine.dispose()


def test_only_owned_non_detached_activities_block_run_completion():
    registry = SessionActivityRegistry()
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-owned",
        kind="background_task",
        run_id="run-1",
    )
    registry.start(
        backend="claude",
        runtime_key="runtime-2",
        session_id="ses-1",
        activity_id="task-detached",
        kind="background_task",
        run_id="run-2",
        detached_from_run=True,
    )

    assert registry.has_blocking_run_activity("run-1") is True
    assert registry.has_blocking_run_activity("run-2") is False

    registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-owned",
        status="completed",
    )
    assert registry.has_blocking_run_activity("run-1") is False


def test_force_end_backend_settles_active_and_discards_pending_output():
    registry = SessionActivityRegistry()
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-active",
        kind="background_task",
    )
    registry.start(
        backend="claude",
        runtime_key="runtime-2",
        session_id="ses-2",
        activity_id="task-complete",
        kind="background_task",
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime-2",
        activity_id="task-complete",
        status="completed",
        expects_output=True,
    )

    assert registry.has_backend_work("claude") is True
    completed = registry.end_backend("claude", status="killed")

    assert sorted((item.id, item.status) for item in completed) == [
        ("task-active", "killed"),
        ("task-complete", "killed"),
    ]
    assert registry.has_backend_work("claude") is False
    assert registry.claim_completed_output("claude", "runtime-2") is None
