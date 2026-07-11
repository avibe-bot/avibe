from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from core.session_activities import SessionActivityRegistry
from core.session_turns import SessionTurnManager


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
        "connection": "connected",
    }

    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None
    assert claimed.id == "task-1"
    assert registry.claim_completed_output("claude", "runtime-1") is None


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
