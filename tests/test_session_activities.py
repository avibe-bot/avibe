from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core.session_activities import (
    SessionActivity,
    SessionActivityRegistry,
    activity_completion_output,
)
from core.session_turns import SessionTurnManager, Turn
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


def test_activity_batch_claim_leaves_interleaved_output_in_place():
    registry = SessionActivityRegistry()
    for activity_id, turn_id in (
        ("task-old", "older-turn"),
        ("task-current-a", "current-turn"),
        ("task-other", "other-turn"),
        ("task-current-b", "current-turn"),
    ):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id=turn_id,
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    claimed = registry.claim_completed_output_batch(
        "claude",
        "runtime-1",
        turn_ids={"current-turn"},
    )

    assert [activity.id for activity in claimed] == [
        "task-current-a",
        "task-current-b",
    ]
    older = registry.claim_completed_output_batch("claude", "runtime-1")
    other = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in older] == ["task-old"]
    assert [activity.id for activity in other] == ["task-other"]
    for activity in [*claimed, *older, *other]:
        registry.ack_completed_output(activity)


def test_activity_batch_requeue_restores_global_fifo_position():
    registry = SessionActivityRegistry()

    def _complete(activity_id: str, turn_id: str) -> None:
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id=turn_id,
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    _complete("task-old", "older-turn")
    _complete("task-current-a", "current-turn")
    _complete("task-other", "other-turn")
    _complete("task-current-b", "current-turn")
    claimed = registry.claim_completed_output_batch(
        "claude",
        "runtime-1",
        turn_ids={"current-turn"},
    )
    _complete("task-new", "newer-turn")

    assert registry.requeue_completed_outputs(claimed) == 2

    restored = []
    while activity := registry.claim_completed_output("claude", "runtime-1"):
        restored.append(activity)
    assert [activity.id for activity in restored] == [
        "task-old",
        "task-current-a",
        "task-other",
        "task-current-b",
        "task-new",
    ]
    for activity in restored:
        registry.ack_completed_output(activity)


def test_activity_output_native_id_is_stable_across_recovery_contexts():
    activity = SessionActivity(
        id="task-1",
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        kind="background_task",
        status="completed",
    )
    output = activity_completion_output(
        activity,
        detached=True,
        completes_turn=False,
    )
    live_context = SimpleNamespace(
        platform_specific={
            "vibe_agent_backend": "claude",
            "agent_session_id": "ses-1",
        }
    )
    recovered_context = SimpleNamespace(
        platform_specific={
            "vibe_agent_backend": "codex",
            "task_execution_id": "activity:claude:task-1",
            "agent_session_id": "ses-1",
        }
    )

    live_id = output.native_message_id(live_context)
    recovered_id = output.native_message_id(recovered_context)

    assert live_id == recovered_id
    assert live_id is not None
    assert live_id.startswith("agent-output:claude:activity:task-1:")
    assert output.requires_delivery_for_run_settlement is True


def test_activity_completion_persistence_failure_keeps_output_unclaimable():
    def upsert_activity(_activity, *, phase):
        if phase == "awaiting_output":
            raise RuntimeError("database is locked")

    store = SimpleNamespace(
        upsert_activity=upsert_activity,
        delete_activity=mock.Mock(),
    )
    registry = SessionActivityRegistry(store)
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
    )

    with pytest.raises(RuntimeError, match="database is locked"):
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id="task-1",
            status="completed",
            expects_output=True,
        )

    assert [item.id for item in registry.active_for_runtime("claude", "runtime-1")] == [
        "task-1"
    ]
    assert registry.has_completed_output("claude", "runtime-1") is False


def test_activity_ack_keeps_claim_until_snapshot_delete_succeeds():
    delete_activity = mock.Mock(side_effect=RuntimeError("database is locked"))
    store = SimpleNamespace(
        upsert_activity=mock.Mock(),
        delete_activity=delete_activity,
    )
    registry = SessionActivityRegistry(store)
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
        expects_output=True,
    )
    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None

    with pytest.raises(RuntimeError, match="database is locked"):
        registry.ack_completed_output(claimed)

    assert registry.has_completed_output("claude", "runtime-1") is True
    delete_activity.side_effect = None
    assert registry.ack_completed_output(claimed) is True
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


def test_turn_state_reports_authoritative_live_owner_diagnostics():
    """HFR-002: queued Run diagnosis comes from the live Session owner."""

    context = SimpleNamespace(
        platform_specific={
            "task_trigger_kind": "agent_run",
            "task_execution_id": "run-owner",
            "agent_runtime_turn_key": "session:/repo",
            "agent_session_target": {"agent_backend": "codex"},
        }
    )
    controller = SimpleNamespace(
        agent_service=SimpleNamespace(
            activities=SessionActivityRegistry(),
            runtime_turn_started=lambda candidate: candidate is context,
        ),
        backend_alive=lambda candidate: False if candidate is context else None,
    )
    manager = SessionTurnManager(controller=controller)
    manager._engine = SimpleNamespace(begin=lambda: nullcontext(object()))
    manager.in_flight["ses-1"] = Turn(
        task=SimpleNamespace(done=lambda: False),
        context=context,
        started_at="2026-07-18T04:31:26+00:00",
    )

    with mock.patch("core.session_turns.messages_service.list_queued", return_value=[{"id": "queued-1"}]):
        state = manager.turn_state("ses-1")

    assert state["in_flight"] is True
    assert state["backend"] == "codex"
    assert state["owner"] == {
        "source": "agent_run",
        "acquired_at": "2026-07-18T04:31:26+00:00",
        "run_id": "run-owner",
        "run_ids": ["run-owner"],
        "runtime_key": "session:/repo",
        "native_turn_started": True,
        "backend_alive": False,
    }


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
    assert len(store.list_activities()) == 1

    restarted_again = SessionActivityRegistry(store)
    repeated = restarted_again.drain_recovered_terminals()
    assert [(item.id, item.status, item.run_id) for item in repeated] == [
        ("task-live", "disconnected", "run-1"),
    ]
    restarted_again.ack_recovered_terminal(repeated[0])
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


def test_force_end_backend_retains_terminal_snapshot_until_ack(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    registry = SessionActivityRegistry(store)
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-active",
        kind="background_task",
    )
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-complete",
        kind="background_task",
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-complete",
        status="completed",
        metadata={"summary": "Do not deliver after forced restart"},
        expects_output=True,
    )
    assert len(store.list_activities()) == 2

    completed = registry.end_backend("claude", status="killed")

    assert sorted((item.id, item.status) for item in completed) == [
        ("task-active", "killed"),
        ("task-complete", "killed"),
    ]
    records = store.list_activities()
    assert len(records) == 2
    assert {record["phase"] for record in records} == {"terminal"}
    assert {record["activity"]["status"] for record in records} == {"killed"}
    recovered = SessionActivityRegistry(store)
    assert recovered.recovered_output_runtimes() == []
    assert recovered.claim_completed_output("claude", "runtime-1") is None
    terminals = recovered.drain_recovered_terminals()
    assert sorted((item.id, item.status) for item in terminals) == [
        ("task-active", "killed"),
        ("task-complete", "killed"),
    ]
    for activity in terminals:
        recovered.ack_recovered_terminal(activity)
    assert store.list_activities() == []
    engine.dispose()


def test_force_end_backend_claimed_output_wins_late_delivery_race(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    registry = SessionActivityRegistry(store)
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-claimed",
        kind="background_task",
        run_id="run-1",
    )
    registry.complete(
        backend="claude",
        runtime_key="runtime-1",
        activity_id="task-claimed",
        status="completed",
        metadata={"summary": "Delivery is in flight"},
        expects_output=True,
    )
    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None
    assert registry.has_backend_work("claude") is True

    completed = registry.end_backend("claude", status="killed")

    assert [(item.id, item.status) for item in completed] == [
        ("task-claimed", "killed"),
    ]
    assert registry.has_backend_work("claude") is False
    assert registry.requeue_completed_output(claimed) is False
    assert registry.ack_completed_output(claimed) is False
    records = store.list_activities()
    assert len(records) == 1
    assert records[0]["phase"] == "terminal"
    assert records[0]["activity"]["status"] == "killed"
    engine.dispose()
