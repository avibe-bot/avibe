from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from sqlalchemy import event

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


def test_hfr_166_recovered_unbound_grace_uses_first_durable_completion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    first = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    for activity_id, completed_at in (
        ("task-a", first),
        ("task-b", first + timedelta(seconds=5)),
    ):
        activity = SessionActivity(
            id=activity_id,
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            kind="background_task",
            status="completed",
            turn_id="turn-1",
            completed_at=completed_at.isoformat(),
            updated_at=completed_at.isoformat(),
        )
        store.upsert_activity(activity.to_dict(), phase="awaiting_output")

    recovered = SessionActivityRegistry(store)
    assert recovered.recovered_output_delay_seconds(
        "claude",
        "runtime-1",
        grace_seconds=10,
        now=first + timedelta(seconds=6),
    ) == 4

    claimed = recovered.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in claimed] == ["task-a", "task-b"]
    assert recovered.requeue_completed_outputs(claimed) == 2
    assert recovered.recovered_output_delay_seconds(
        "claude",
        "runtime-1",
        grace_seconds=10,
        now=first + timedelta(seconds=6),
    ) == 0
    engine.dispose()


def test_hfr_166_recovered_runtime_scan_applies_keyset_limit_in_registry() -> None:
    completed_at = datetime.now(timezone.utc).isoformat()

    class _Store:
        @staticmethod
        def list_activities():
            return [
                {
                    "activity": SessionActivity(
                        id=f"activity-{index}",
                        backend="claude",
                        runtime_key=f"runtime-{index:03d}",
                        session_id=f"session-{index}",
                        kind="background_task",
                        status="completed",
                        completed_at=completed_at,
                        metadata={"output_batch_id": f"batch-{index}"},
                    ).to_dict(),
                    "phase": "awaiting_output",
                }
                for index in range(100)
            ]

    grace_calls = 0

    def grace_seconds(_backend: str) -> float:
        nonlocal grace_calls
        grace_calls += 1
        return 10.0

    registry = SessionActivityRegistry(_Store())
    page, has_more, retry_after, scanned_cursor = (
        registry.scan_recovered_output_runtimes(
            limit=2,
            cursor=None,
            grace_seconds=grace_seconds,
        )
    )

    assert page == [
        ("claude", "runtime-000"),
        ("claude", "runtime-001"),
    ]
    assert has_more is True
    assert retry_after is None
    assert scanned_cursor == "claude\x1fruntime-001"
    assert grace_calls == 2

    next_page, has_more, _retry_after, scanned_cursor = (
        registry.scan_recovered_output_runtimes(
            limit=2,
            cursor="claude\x1fruntime-001",
            grace_seconds=grace_seconds,
        )
    )
    assert next_page == [
        ("claude", "runtime-002"),
        ("claude", "runtime-003"),
    ]
    assert has_more is True
    assert scanned_cursor == "claude\x1fruntime-003"
    assert grace_calls == 4


def test_hfr_285_deferred_runtime_scan_bounds_raw_rows_and_indexes_deadline() -> None:
    completed_at = datetime.now(timezone.utc).isoformat()

    class _Store:
        @staticmethod
        def list_activities():
            return [
                {
                    "activity": SessionActivity(
                        id=f"activity-{index}",
                        backend="claude",
                        runtime_key=f"runtime-{index:03d}",
                        session_id=f"session-{index}",
                        kind="background_task",
                        status="completed",
                        completed_at=completed_at,
                    ).to_dict(),
                    "phase": "awaiting_output",
                }
                for index in range(100)
            ]

    grace_calls = 0

    def grace_seconds(_backend: str) -> float:
        nonlocal grace_calls
        grace_calls += 1
        return 30.0

    registry = SessionActivityRegistry(_Store())
    page, has_more, retry_after, scanned_cursor = (
        registry.scan_recovered_output_runtimes(
            limit=2,
            cursor=None,
            grace_seconds=grace_seconds,
        )
    )

    assert page == []
    assert has_more is True
    assert retry_after is not None and 0 < retry_after <= 30
    assert scanned_cursor == "claude\x1fruntime-001"
    assert grace_calls == 3


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
    for activity in claimed:
        registry.ack_completed_output(activity)
    older = registry.claim_completed_output_batch("claude", "runtime-1")
    for activity in older:
        registry.ack_completed_output(activity)
    other = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in older] == ["task-old"]
    assert [activity.id for activity in other] == ["task-other"]
    for activity in other:
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
        registry.ack_completed_output(activity)
    assert [activity.id for activity in restored] == [
        "task-old",
        "task-current-b",
        "task-other",
        "task-new",
    ]


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
    assert live_id.startswith(
        "agent-output:claude:activity-batch:claude:runtime-1:activity:task-1:"
    )
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


def test_activity_ack_replaces_undeletable_snapshot_with_terminal_evidence():
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

    assert registry.ack_completed_output(claimed) is True

    assert registry.has_completed_output("claude", "runtime-1") is False
    assert store.upsert_activity.call_args.kwargs["phase"] == "terminal"


def test_delivered_output_without_durable_evidence_stays_claimed_when_terminal_write_fails():
    callback = mock.Mock()
    store = SimpleNamespace(
        upsert_activity=mock.Mock(),
        delete_activity=mock.Mock(),
    )
    registry = SessionActivityRegistry(store)
    registry.set_output_settled_callback(callback)
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

    with (
        mock.patch.object(
            registry,
            "_delete_activity",
            side_effect=RuntimeError("activity delete unavailable"),
        ),
        mock.patch.object(
            registry,
            "_persist_activity",
            side_effect=RuntimeError("terminal write unavailable"),
        ),
    ):
        assert registry.settle_completed_output_delivery(
            claimed,
            accepted_message_exists=False,
        ) is False
        assert registry.ack_completed_output(claimed) is False

    assert registry.has_completed_output("claude", "runtime-1") is True
    assert registry.claim_completed_output("claude", "runtime-1") is None
    callback.assert_not_called()


def test_terminal_callback_is_not_durable_delivery_evidence():
    callback = mock.Mock()
    registry = SessionActivityRegistry()
    registry.set_output_settled_callback(callback)
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
        run_id="run-1",
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
    terminal = registry.delivered_output_failure(
        claimed,
        RuntimeError("run store unavailable"),
    )
    settle_terminal = mock.Mock(return_value=True)

    with mock.patch.object(
        registry,
        "_persist_activity",
        side_effect=RuntimeError("terminal write unavailable"),
    ):
        assert registry.settle_completed_output_delivery(
            claimed,
            accepted_message_exists=False,
            terminal_activity=terminal,
            settle_terminal=settle_terminal,
        ) is False

    settle_terminal.assert_called_once_with(terminal)
    assert registry.has_completed_output("claude", "runtime-1") is True
    assert registry.claim_completed_output("claude", "runtime-1") is None
    callback.assert_not_called()


def test_terminal_evidence_requeues_only_for_local_settlement_retry():
    callback = mock.Mock()
    store = SimpleNamespace(
        upsert_activity=mock.Mock(),
        delete_activity=mock.Mock(),
    )
    registry = SessionActivityRegistry(store)
    registry.set_output_settled_callback(callback)
    registry.start(
        backend="claude",
        runtime_key="runtime-1",
        session_id="ses-1",
        activity_id="task-1",
        kind="background_task",
        run_id="run-1",
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
    output = activity_completion_output(
        claimed,
        detached=True,
        completes_turn=False,
    )
    settle_terminal = mock.Mock(side_effect=[False, True])

    assert registry.settle_completed_output_batch(
        output,
        accepted_message_exists=False,
        settlement_error=RuntimeError("run store unavailable"),
        settle_terminal=settle_terminal,
    ) is False
    assert registry.requeue_completed_outputs([claimed]) == 0
    assert registry.requeue_completed_output(claimed, recovered=True) is True

    representative = registry.claim_completed_output(
        "claude",
        "runtime-1",
        recovered_only=True,
    )
    assert representative is not None
    retried = registry.claimed_completed_output_batch_for_output(
        activity_completion_output(
            representative,
            detached=True,
            completes_turn=False,
        )
    )
    assert [activity.id for activity in retried] == ["task-1"]
    retry_output = activity_completion_output(
        retried[-1],
        activities=retried,
        detached=True,
        completes_turn=False,
    )
    assert retry_output.metadata["activity_local_settlement_only"] is True
    assert registry.settle_completed_output_batch(
        retry_output,
        accepted_message_exists=False,
        settlement_error=RuntimeError("retry local settlement"),
        settle_terminal=settle_terminal,
    ) is True

    assert settle_terminal.call_count == 2
    callback.assert_called_once()
    assert registry.has_completed_output("claude", "runtime-1") is False


def test_activity_batch_receipt_is_stable_for_every_member_after_restart():
    registry = SessionActivityRegistry()
    for activity_id, run_id in (("task-a", "run-a"), ("task-b", "run-b")):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
            run_id=run_id,
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )
    claimed = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in claimed] == ["task-a", "task-b"]

    batch_output = activity_completion_output(
        claimed[-1],
        activities=claimed,
        detached=True,
        completes_turn=False,
    )
    recovered_output = activity_completion_output(
        claimed[0],
        detached=True,
        completes_turn=False,
    )
    context = SimpleNamespace(
        platform_specific={"vibe_agent_backend": "claude"},
    )

    assert batch_output.activity_ids == ("task-a", "task-b")
    assert batch_output.run_ids == ("run-a", "run-b")
    assert batch_output.native_message_id(context) == recovered_output.native_message_id(
        context
    )


def test_failed_batch_binding_restores_every_claim_to_the_retryable_queue():
    store = SimpleNamespace(
        upsert_activity=mock.Mock(),
        upsert_activities=mock.Mock(
            side_effect=RuntimeError("batch binding unavailable")
        ),
    )
    registry = SessionActivityRegistry(store)
    for activity_id in ("task-a", "task-b"):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    with pytest.raises(RuntimeError, match="batch binding unavailable"):
        registry.claim_completed_output_batch("claude", "runtime-1")

    assert registry.has_completed_output("claude", "runtime-1") is True
    store.upsert_activities.side_effect = None
    retried = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in retried] == ["task-a", "task-b"]
    assert len({activity.metadata["output_batch_id"] for activity in retried}) == 1


def test_durable_store_without_bulk_binding_never_falls_back_to_member_writes():
    store = SimpleNamespace(upsert_activity=mock.Mock())
    registry = SessionActivityRegistry(store)
    for activity_id in ("task-a", "task-b"):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    with pytest.raises(RuntimeError, match="cannot atomically bind"):
        registry.claim_completed_output_batch("claude", "runtime-1")

    assert registry.has_completed_output("claude", "runtime-1") is True
    assert store.upsert_activity.call_count == 4


def test_partial_batch_binding_recovers_as_one_complete_retryable_batch():
    class _Store:
        def __init__(self):
            self.records = {}
            self.binding_writes = 0
            self.fail_second_binding = True

        def upsert_activity(self, activity, *, phase):
            self.records[activity["id"]] = {
                "activity": dict(activity),
                "phase": phase,
            }

        def upsert_activities(self, activities, *, phase):
            self.binding_writes += len(activities)
            if self.fail_second_binding:
                raise RuntimeError("second batch member unavailable")
            for activity in activities:
                self.upsert_activity(activity, phase=phase)

        def list_activities(self):
            return list(self.records.values())

    store = _Store()
    registry = SessionActivityRegistry(store)
    for activity_id, run_id in (("task-a", "run-a"), ("task-b", "run-b")):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
            run_id=run_id,
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    with pytest.raises(RuntimeError, match="second batch member unavailable"):
        registry.claim_completed_output_batch("claude", "runtime-1")

    store.fail_second_binding = False
    retried = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in retried] == ["task-a", "task-b"]
    representative = retried[-1]
    output = activity_completion_output(
        representative,
        activities=retried,
        detached=True,
        completes_turn=False,
    )

    assert output.activity_ids == ("task-a", "task-b")
    assert output.run_ids == ("run-a", "run-b")
    assert [
        activity.id
        for activity in registry.claimed_completed_output_batch_for_output(output)
    ] == ["task-a", "task-b"]


def test_sqlite_batch_binding_rolls_back_a_later_member_failure(tmp_path: Path):
    db_path = tmp_path / "state" / "vibe.sqlite"
    ensure_sqlite_state(db_path=db_path, primary_platform="avibe")
    engine = create_sqlite_engine(db_path)
    store = SQLiteSessionActivityStore(engine)
    originals = [
        SessionActivity(
            id=activity_id,
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            kind="background_task",
            status="completed",
            turn_id="turn-1",
            run_id=run_id,
            metadata={"summary": activity_id},
        ).to_dict()
        for activity_id, run_id in (("task-a", "run-a"), ("task-b", "run-b"))
    ]
    for activity in originals:
        store.upsert_activity(activity, phase="awaiting_output")
    bound = [
        {
            **activity,
            "metadata": {
                **activity["metadata"],
                "output_batch_id": "batch-1",
                "output_batch_activity_ids": ["task-a", "task-b"],
                "output_batch_run_ids": ["run-a", "run-b"],
            },
        }
        for activity in originals
    ]
    writes = 0

    def fail_second_write(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal writes
        if statement.lstrip().upper().startswith("INSERT INTO RUNTIME_RECORDS"):
            writes += 1
            if writes == 2:
                raise RuntimeError("second batch member unavailable")

    event.listen(engine, "before_cursor_execute", fail_second_write)
    try:
        with pytest.raises(RuntimeError, match="second batch member unavailable"):
            store.upsert_activities(bound, phase="awaiting_output")
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_write)

    records = store.list_activities()
    assert [record["activity"]["id"] for record in records] == ["task-a", "task-b"]
    assert all(
        "output_batch_id" not in record["activity"]["metadata"]
        for record in records
    )
    engine.dispose()


def test_single_recovery_claim_expands_a_persisted_output_batch():
    class _Store:
        def __init__(self):
            self.records = {}

        def upsert_activity(self, activity, *, phase):
            self.records[activity["id"]] = {
                "activity": dict(activity),
                "phase": phase,
            }

        def upsert_activities(self, activities, *, phase):
            for activity in activities:
                self.upsert_activity(activity, phase=phase)

        def list_activities(self):
            return list(self.records.values())

    store = _Store()
    registry = SessionActivityRegistry(store)
    for activity_id, run_id in (("task-a", "run-a"), ("task-b", "run-b")):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
            run_id=run_id,
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )
    registry.claim_completed_output_batch("claude", "runtime-1")

    recovered = SessionActivityRegistry(store)
    representative = recovered.claim_completed_output(
        "claude",
        "runtime-1",
        recovered_only=True,
    )
    assert representative is not None
    output = activity_completion_output(
        representative,
        detached=True,
        completes_turn=False,
    )

    assert output.activity_ids == ("task-a", "task-b")
    assert output.run_ids == ("run-a", "run-b")
    assert [
        activity.id
        for activity in recovered.claimed_completed_output_batch_for_output(output)
    ] == ["task-a", "task-b"]

    assert recovered.requeue_completed_output(representative, recovered=True)
    retried = recovered.claim_completed_output(
        "claude",
        "runtime-1",
        recovered_only=True,
    )
    assert retried is not None
    retried_output = activity_completion_output(
        retried,
        detached=True,
        completes_turn=False,
    )
    assert [
        activity.id
        for activity in recovered.claimed_completed_output_batch_for_output(
            retried_output
        )
    ] == ["task-a", "task-b"]


def test_recovery_marks_an_incomplete_persisted_output_batch_for_fail_closed_emit():
    store = SimpleNamespace(
        upsert_activity=mock.Mock(),
        upsert_activities=mock.Mock(),
        list_activities=mock.Mock(
            return_value=[
                {
                    "phase": "awaiting_output",
                    "activity": SessionActivity(
                        id="task-a",
                        backend="claude",
                        runtime_key="runtime-1",
                        session_id="ses-1",
                        kind="background_task",
                        status="completed",
                        turn_id="turn-1",
                        run_id="run-a",
                        metadata={
                            "summary": "must not send",
                            "output_batch_id": "batch-1",
                            "output_batch_activity_ids": ["task-a", "task-b"],
                            "output_batch_run_ids": ["run-a", "run-b"],
                        },
                    ).to_dict(),
                }
            ]
        ),
    )
    registry = SessionActivityRegistry(store)

    representative = registry.claim_completed_output(
        "claude",
        "runtime-1",
        recovered_only=True,
    )
    assert representative is not None
    output = activity_completion_output(
        representative,
        detached=True,
        completes_turn=False,
    )
    assert output.activity_ids == ("task-a", "task-b")
    assert output.metadata["activity_batch_complete"] is False

    assert registry.requeue_completed_output(representative, recovered=True)
    assert registry.has_completed_output("claude", "runtime-1") is True
    assert registry.claim_completed_output_batch(
        "claude",
        "runtime-1",
        turn_ids={"different-turn"},
    ) == []


def test_requeued_bound_batch_does_not_absorb_later_same_turn_completion():
    registry = SessionActivityRegistry()

    def complete(activity_id: str) -> None:
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )

    complete("task-a")
    first = registry.claim_completed_output_batch("claude", "runtime-1")
    first_batch_id = first[0].metadata["output_batch_id"]
    assert registry.requeue_completed_outputs(first) == 1
    complete("task-b")

    retried = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in retried] == ["task-a"]
    assert retried[0].metadata["output_batch_id"] == first_batch_id
    assert registry.settle_completed_output_batch(
        activity_completion_output(
            retried[0],
            activities=retried,
            detached=True,
            completes_turn=False,
        ),
        accepted_message_exists=True,
    ) is True

    later = registry.claim_completed_output_batch("claude", "runtime-1")
    assert [activity.id for activity in later] == ["task-b"]
    assert later[0].metadata["output_batch_id"] != first_batch_id


def test_batch_callbacks_run_after_all_claims_release_and_outside_registry_lock():
    registry = SessionActivityRegistry()
    for activity_id in ("task-a", "task-b"):
        registry.start(
            backend="claude",
            runtime_key="runtime-1",
            session_id="ses-1",
            activity_id=activity_id,
            kind="background_task",
            turn_id="turn-1",
        )
        registry.complete(
            backend="claude",
            runtime_key="runtime-1",
            activity_id=activity_id,
            status="completed",
            expects_output=True,
        )
    claimed = registry.claim_completed_output_batch("claude", "runtime-1")
    output = activity_completion_output(
        claimed[-1],
        activities=claimed,
        detached=True,
        completes_turn=False,
    )
    observations = []

    def callback(activity):
        inspected = threading.Event()

        def inspect_registry():
            observations.append(
                (
                    activity.id,
                    registry.has_completed_output("claude", "runtime-1"),
                )
            )
            inspected.set()

        worker = threading.Thread(target=inspect_registry)
        worker.start()
        worker.join(timeout=0.2)
        assert inspected.is_set()

    registry.set_output_settled_callback(callback)

    assert registry.settle_completed_output_batch(
        output,
        accepted_message_exists=True,
    ) is True
    assert observations == [("task-a", False), ("task-b", False)]


def test_delivered_output_settlement_invokes_callback_outside_registry_lock():
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
        expects_output=True,
    )
    claimed = registry.claim_completed_output("claude", "runtime-1")
    assert claimed is not None
    callback_observations = []

    def callback(_activity):
        inspected = threading.Event()

        def inspect_registry():
            registry.has_completed_output("claude", "runtime-1")
            inspected.set()

        worker = threading.Thread(target=inspect_registry)
        worker.start()
        worker.join(timeout=0.2)
        callback_observations.append(inspected.is_set())
        worker.join(timeout=0.2)

    registry.set_output_settled_callback(callback)

    assert registry.settle_completed_output_delivery(
        claimed,
        accepted_message_exists=False,
    ) is True
    assert callback_observations == [True]


def test_claimed_output_mutation_is_owned_by_the_registry_api():
    repo_root = Path(__file__).resolve().parents[1]

    for relative_path in (
        "core/message_dispatcher.py",
        "modules/agents/claude_agent.py",
    ):
        source = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "_claimed_completed_outputs" not in source

    claude_source = (repo_root / "modules/agents/claude_agent.py").read_text(
        encoding="utf-8"
    )
    assert ".ack_completed_output(" not in claude_source


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
        "core.session_turns.delivery_store.list_queued",
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

    with mock.patch("core.session_turns.delivery_store.list_queued", return_value=[{"id": "queued-1"}]):
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
