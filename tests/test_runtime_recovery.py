from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from core.runtime_recovery import (
    FallbackRequestRecoveryHandler,
    SessionDeliveryRecoveryHandler,
)
from core.runtime_work import RuntimeWorkItem, RuntimeWorkLane, RuntimeWorkSupervisor
from core.session_turns import SessionTurnManager
from storage import message_deliveries as delivery_store
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.models import agent_runs, agent_sessions, metadata


NOW = "2026-08-03T00:00:00+00:00"


def _engine(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "runtime-recovery.sqlite")
    metadata.create_all(engine)
    return engine


def _session(conn, session_id: str, *, backend: str = "codex") -> None:
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=None,
            agent_id=None,
            agent_name=backend,
            agent_backend=backend,
            agent_variant=backend,
            model=None,
            reasoning_effort=None,
            session_anchor=f"base-{session_id}",
            workdir="/work",
            native_session_id="",
            title=None,
            status="active",
            visibility="foreground",
            pinned=0,
            agent_status="idle",
            metadata_json="{}",
            created_at=NOW,
            updated_at=NOW,
            last_active_at=NOW,
        )
    )


def _delivery(
    conn,
    delivery_id: str,
    session_id: str,
    *,
    state: str = "queued",
) -> None:
    delivery_store.insert_delivery(
        conn,
        delivery_id=delivery_id,
        session_id=session_id,
        priority="p3",
        state=state,
        snapshot=delivery_store.message_snapshot(
            scope_id=None,
            session_id=session_id,
            platform="avibe",
            author="user",
            source="user",
            text=delivery_id,
        ),
        dispatch_text=delivery_id,
        turn_id=None,
    )


def _linked_waiting_successor(conn, session_id: str) -> tuple[str, str, str]:
    predecessor_id = f"turn-predecessor-{session_id}"
    predecessor_delivery_id = f"delivery-predecessor-{session_id}"
    successor_id = f"turn-successor-{session_id}"
    successor_delivery_id = f"delivery-successor-{session_id}"
    _delivery(conn, predecessor_delivery_id, session_id)
    delivery_store.insert_turn(
        conn,
        turn_id=predecessor_id,
        session_id=session_id,
        initial_delivery_id=predecessor_delivery_id,
        state="active",
        backend="codex",
        dispatch_text="predecessor",
    )
    conn.execute(
        update(delivery_store.message_deliveries)
        .where(delivery_store.message_deliveries.c.id == predecessor_delivery_id)
        .values(
            state="claimed",
            turn_id=predecessor_id,
            turn_role="initial",
            turn_position=0,
        )
    )
    _delivery(conn, successor_delivery_id, session_id)
    delivery_store.insert_turn(
        conn,
        turn_id=successor_id,
        session_id=session_id,
        initial_delivery_id=successor_delivery_id,
        state="waiting",
        backend="codex",
    )
    conn.execute(
        update(delivery_store.message_deliveries)
        .where(delivery_store.message_deliveries.c.id == successor_delivery_id)
        .values(
            state="interrupt_waiting",
            turn_id=successor_id,
            turn_role="initial",
            turn_position=0,
        )
    )
    predecessor = delivery_store.get_turn(conn, predecessor_id)
    assert predecessor is not None
    assert delivery_store.cas_turn(
        conn,
        predecessor_id,
        expected_version=int(predecessor["version"]),
        expected_states=("active",),
        values={
            "control_state": "pending",
            "control_mode": "replace",
            "control_attempt_id": f"control-{session_id}",
            "control_expected_native_turn_id": None,
            "control_receipt_outcome": None,
            "control_receipt_json": "{}",
            "control_successor_delivery_id": successor_delivery_id,
            "control_successor_turn_id": successor_id,
        },
    )
    return predecessor_id, successor_id, successor_delivery_id


@pytest.mark.anyio
async def test_hfr_149_session_lane_recovers_only_current_exact_observation(
    tmp_path: Path,
) -> None:
    """HFR-149: periodic Session recovery re-enters only an exact observation."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-current")
        _delivery(conn, "delivery-current", "ses-current")
        queued = delivery_store.get_delivery(conn, "delivery-current")
        delivery_store.claim_start_batch(
            conn,
            turn_id="turn-current",
            session_id="ses-current",
            backend="codex",
            deliveries=[queued],
            dispatch_text="delivery-current",
            attempt_id="attempt-current",
        )
        _session(conn, "ses-stale")
        _delivery(conn, "delivery-stale", "ses-stale")

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    manager._start_persisted_turn = AsyncMock(return_value=True)
    handler = SessionDeliveryRecoveryHandler(manager)
    items, _has_more = handler.scan(limit=10, occupied=frozenset())
    by_session = {item.partition_key: item for item in items}
    assert {"ses-current", "ses-stale"}.issubset(by_session)

    with engine.begin() as conn:
        conn.execute(
            update(delivery_store.message_deliveries)
            .where(delivery_store.message_deliveries.c.id == "delivery-stale")
            .values(version=2)
        )
    await handler.process(by_session["ses-stale"])
    manager._start_persisted_turn.assert_not_awaited()
    with engine.connect() as conn:
        assert delivery_store.get_delivery(conn, "delivery-stale")["state"] == "queued"

    await handler.process(by_session["ses-current"])
    manager._start_persisted_turn.assert_awaited_once_with(
        "turn-current",
        expected_start_attempt_id="attempt-current",
    )
    engine.dispose()


def test_hfr_149_periodic_session_scan_finds_every_unresolved_owner(
    tmp_path: Path,
) -> None:
    """HFR-149: bounded scans find fences plus starting and waiting Turns."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-fence")
        _delivery(conn, "delivery-fence", "ses-fence", state="reserved")

        _session(conn, "ses-starting")
        _delivery(conn, "delivery-starting", "ses-starting")
        starting = delivery_store.get_delivery(conn, "delivery-starting")
        delivery_store.claim_start_batch(
            conn,
            turn_id="turn-starting",
            session_id="ses-starting",
            backend="codex",
            deliveries=[starting],
            dispatch_text="delivery-starting",
            attempt_id="attempt-starting",
        )

        _session(conn, "ses-waiting")
        _delivery(conn, "delivery-waiting", "ses-waiting")
        waiting_delivery = delivery_store.get_delivery(conn, "delivery-waiting")
        delivery_store.insert_turn(
            conn,
            turn_id="turn-waiting",
            session_id="ses-waiting",
            initial_delivery_id="delivery-waiting",
            state="waiting",
            backend="codex",
        )
        assert delivery_store.cas_delivery(
            conn,
            "delivery-waiting",
            expected_version=int(waiting_delivery["version"]),
            expected_states=("queued",),
            values={
                "state": "interrupt_waiting",
                "turn_id": "turn-waiting",
                "turn_role": "initial",
                "turn_position": 0,
            },
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    observations, has_more = manager.scan_runtime_delivery_recovery(
        limit=10,
        occupied=frozenset(),
    )
    by_session = {observation.session_id: observation for observation in observations}

    assert not has_more
    assert by_session["ses-fence"].kind == "delivery_fence"
    assert by_session["ses-starting"].turn_state == "starting"
    assert by_session["ses-waiting"].turn_state == "waiting"
    engine.dispose()


def test_hfr_151_session_scan_advances_with_a_bounded_keyset_cursor(
    tmp_path: Path,
) -> None:
    """HFR-151: Session recovery pages by its stable partition key."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        for index in range(1, 7):
            session_id = f"ses-{index}"
            _session(conn, session_id)
            _delivery(conn, f"delivery-{index}", session_id, state="reserved")

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    first, first_has_more = manager.scan_runtime_delivery_recovery(
        limit=4,
        occupied=frozenset(),
        cursor=None,
    )
    second, second_has_more = manager.scan_runtime_delivery_recovery(
        limit=4,
        occupied=frozenset(),
        cursor=first[-1].session_id,
    )

    assert [item.session_id for item in first] == [
        "ses-1",
        "ses-2",
        "ses-3",
        "ses-4",
    ]
    assert first_has_more
    assert [item.session_id for item in second] == ["ses-5", "ses-6"]
    assert not second_has_more
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_134_hfr_149_waiting_recovery_freezes_terminal_winner(
    tmp_path: Path,
) -> None:
    """HFR-134/HFR-149: only a current terminal winner starts its exact successor."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-linked")
        predecessor_id, successor_id, successor_delivery_id = (
            _linked_waiting_successor(conn, "ses-linked")
        )
        _delivery(conn, "delivery-follower", "ses-linked")

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    manager._start_persisted_turn = AsyncMock(return_value=True)
    manager._run_pending_interrupt = AsyncMock(return_value=True)

    before_terminal, _ = manager.scan_runtime_delivery_recovery(
        limit=10,
        occupied=frozenset(),
    )
    observed_before_terminal = next(
        item for item in before_terminal if item.session_id == "ses-linked"
    )
    assert observed_before_terminal.turn_id == successor_id
    assert observed_before_terminal.predecessor_turn_id == predecessor_id
    assert observed_before_terminal.predecessor_state == "active"
    assert await manager.recover_runtime_delivery_observation(
        observed_before_terminal
    )
    manager._run_pending_interrupt.assert_awaited_once_with(
        "ses-linked",
        predecessor_id,
        expected_state="active",
        expected_version=observed_before_terminal.predecessor_version,
        expected_control_state="pending",
        expected_control_attempt_id="control-ses-linked",
        expected_native_turn_id=None,
        expected_successor_delivery_id=successor_delivery_id,
        expected_successor_turn_id=successor_id,
    )
    manager._start_persisted_turn.assert_not_awaited()
    with engine.connect() as conn:
        assert delivery_store.get_turn(conn, successor_id)["state"] == "waiting"

    with engine.begin() as conn:
        terminal = delivery_store.terminalize_turn(
            conn,
            predecessor_id,
            outcome="canceled",
            settled_by="test_terminal_winner",
            evidence_kind="test_terminal_winner",
            evidence={"winner": True},
        )
        assert terminal["changed"]
    after_terminal, _ = manager.scan_runtime_delivery_recovery(
        limit=10,
        occupied=frozenset(),
    )
    stale_terminal = next(
        item for item in after_terminal if item.session_id == "ses-linked"
    )
    assert stale_terminal.predecessor_state == "terminal"
    assert stale_terminal.predecessor_terminal_outcome == "canceled"
    with engine.begin() as conn:
        predecessor = delivery_store.get_turn(conn, predecessor_id)
        conn.execute(
            update(delivery_store.session_turns)
            .where(delivery_store.session_turns.c.id == predecessor_id)
            .values(version=int(predecessor["version"]) + 1)
        )
    assert not await manager.recover_runtime_delivery_observation(stale_terminal)
    manager._start_persisted_turn.assert_not_awaited()

    current_rows, _ = manager.scan_runtime_delivery_recovery(
        limit=10,
        occupied=frozenset(),
    )
    current = next(item for item in current_rows if item.session_id == "ses-linked")
    assert await manager.recover_runtime_delivery_observation(current)
    with engine.connect() as conn:
        successor = delivery_store.get_turn(conn, successor_id)
        successor_delivery = delivery_store.get_delivery(conn, successor_delivery_id)
        follower = delivery_store.get_delivery(conn, "delivery-follower")
    assert successor["state"] == "starting"
    assert successor_delivery["state"] == "claimed"
    assert follower["state"] == "queued"
    manager._start_persisted_turn.assert_awaited_once_with(
        successor_id,
        expected_start_attempt_id=successor["start_attempt_id"],
    )
    assert not await manager.recover_runtime_delivery_observation(current)
    assert manager._start_persisted_turn.await_count == 1
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_149_periodic_requests_lane_requeues_only_pre_execution_claim(
    tmp_path: Path,
) -> None:
    """HFR-149: PR3 periodically recovers only fallback claims before PID start."""
    db_path = tmp_path / "fallback.sqlite"
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(
            {
                "id": "run-pre-execution",
                "run_type": "scheduled",
                "status": "processing",
                "agent_name": "codex",
                "agent_backend": "codex",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.enqueue_run(
            {
                "id": "run-started",
                "run_type": "scheduled",
                "status": "running",
                "agent_name": "codex",
                "agent_backend": "codex",
                "pid": 321,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        store.enqueue_run(
            {
                "id": "run-unknown-type",
                "run_type": "unknown_execution",
                "status": "running",
                "agent_name": "codex",
                "agent_backend": "codex",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        supervisor = RuntimeWorkSupervisor(
            reconcile_interval=0.01,
            lane_capacity=1,
        )
        processed = asyncio.Event()

        class _ObservedFallbackRequestRecoveryHandler(
            FallbackRequestRecoveryHandler
        ):
            async def process(self, item):
                result = await super().process(item)
                processed.set()
                return result

        supervisor.register(
            RuntimeWorkLane.REQUESTS,
            _ObservedFallbackRequestRecoveryHandler(store),
        )
        await supervisor.activate()
        await asyncio.wait_for(processed.wait(), timeout=1)
        await supervisor.stop()

        with store.engine.connect() as conn:
            rows = {
                row["id"]: dict(row)
                for row in conn.execute(
                    select(agent_runs).where(
                        agent_runs.c.id.in_(
                            (
                                "run-pre-execution",
                                "run-started",
                                "run-unknown-type",
                            )
                        )
                    )
                ).mappings()
            }
        assert rows["run-pre-execution"]["status"] == "queued"
        assert rows["run-pre-execution"]["pid"] is None
        assert rows["run-started"]["status"] == "running"
        assert rows["run-started"]["pid"] == 321
        assert rows["run-unknown-type"]["status"] == "running"
        assert rows["run-unknown-type"]["pid"] is None
    finally:
        store.close()


@pytest.mark.anyio
async def test_hfr_149_fallback_recovery_preserves_the_live_service_claim(
    tmp_path: Path,
) -> None:
    """HFR-149: rolling refresh cannot requeue the loop-owned active claim."""

    store = SQLiteBackgroundTaskStore(tmp_path / "live-claim.sqlite")
    live_claims = frozenset({"run-live"})
    try:
        store.enqueue_run(
            {
                "id": "run-live",
                "run_type": "scheduled",
                "status": "running",
                "agent_name": "codex",
                "agent_backend": "codex",
                "pid": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        handler = FallbackRequestRecoveryHandler(
            store,
            live_claims=lambda: live_claims,
        )

        hidden, has_more = handler.scan(
            limit=10,
            occupied=frozenset(),
        )
        assert hidden == []
        assert has_more is False
        assert store.get_run("run-live")["status"] == "running"

        live_claims = frozenset()
        visible, has_more = handler.scan(
            limit=10,
            occupied=frozenset(),
        )
        assert [item.partition_key for item in visible] == ["run-live"]
        assert has_more is False
        assert await handler.process(visible[0])
        assert store.get_run("run-live")["status"] == "queued"
    finally:
        store.close()


@pytest.mark.anyio
async def test_fallback_recovery_write_runs_off_the_controller_loop() -> None:
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    class _Store:
        @staticmethod
        def recover_claimed_pre_execution_run(**_kwargs) -> bool:
            worker_threads.append(threading.get_ident())
            return True

    handler = FallbackRequestRecoveryHandler(_Store())
    item = RuntimeWorkItem(
        "run-1",
        {
            "id": "run-1",
            "status": "running",
            "updated_at": NOW,
        },
    )

    assert await handler.process(item)
    assert worker_threads and worker_threads[0] != loop_thread


def test_hfr_151_fallback_scan_advances_with_a_bounded_keyset_cursor(
    tmp_path: Path,
) -> None:
    """HFR-151: fallback recovery pages by its stable partition key."""

    store = SQLiteBackgroundTaskStore(tmp_path / "fallback-cursor.sqlite")
    try:
        for index in range(1, 7):
            store.enqueue_run(
                {
                    "id": f"run-{index}",
                    "run_type": "scheduled",
                    "status": "processing",
                    "agent_name": "codex",
                    "agent_backend": "codex",
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )

        first, first_has_more = store.scan_claimed_pre_execution_runs(
            limit=4,
            occupied=frozenset(),
            cursor=None,
        )
        second, second_has_more = store.scan_claimed_pre_execution_runs(
            limit=4,
            occupied=frozenset(),
            cursor=str(first[-1]["id"]),
        )

        assert [row["id"] for row in first] == [
            "run-1",
            "run-2",
            "run-3",
            "run-4",
        ]
        assert first_has_more
        assert [row["id"] for row in second] == ["run-5", "run-6"]
        assert not second_has_more
    finally:
        store.close()


@pytest.mark.anyio
async def test_cancel_settles_durable_owner_when_runtime_is_gone(tmp_path: Path) -> None:
    """Stop must terminalize a durable owner after the in-memory poll is gone."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-zombie", backend="opencode")
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses-zombie")
            .values(agent_status="running")
        )
        _delivery(conn, "delivery-zombie", "ses-zombie")
        delivery_store.insert_turn(
            conn,
            turn_id="trn-zombie",
            session_id="ses-zombie",
            initial_delivery_id="delivery-zombie",
            state="active",
            backend="opencode",
            dispatch_text="stuck",
        )
        conn.execute(
            update(delivery_store.message_deliveries)
            .where(delivery_store.message_deliveries.c.id == "delivery-zombie")
            .values(
                state="claimed",
                turn_id="trn-zombie",
                turn_role="initial",
                turn_position=0,
            )
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    result = await manager.cancel("ses-zombie")

    assert result["ok"] is True
    assert result["status"] == "stale_released"
    assert result["reason"] == "runtime_gone"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, "trn-zombie")
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == "ses-zombie")
        ).mappings().one()
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "canceled"
    assert turn["settled_by"] == "stopped"
    assert turn["terminal_evidence_kind"] == "runtime_gone"
    assert session["agent_status"] != "running"


@pytest.mark.anyio
async def test_cancel_keeps_live_memory_task_on_interrupt_path(tmp_path: Path) -> None:
    """A still-running in-memory task must not skip to runtime-gone settlement."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-live", backend="opencode")
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses-live")
            .values(agent_status="running")
        )
        _delivery(conn, "delivery-live", "ses-live")
        delivery_store.insert_turn(
            conn,
            turn_id="trn-live",
            session_id="ses-live",
            initial_delivery_id="delivery-live",
            state="active",
            backend="opencode",
            dispatch_text="live",
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    seen = {}

    async def _deliver(request, *, context=None):
        seen["priority"] = request.priority
        seen["turn_id"] = request.expected_turn_id
        return SimpleNamespace(state="waiting_terminal", reason=None)

    manager.deliver = _deliver
    manager.in_flight["ses-live"] = SimpleNamespace(
        task=SimpleNamespace(done=lambda: False),
        context=SimpleNamespace(),
    )
    result = await manager.cancel("ses-live")

    assert result == {
        "ok": True,
        "session_id": "ses-live",
        "status": "cancel_requested",
    }
    assert seen == {"priority": "p0", "turn_id": "trn-live"}
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, "trn-live")
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == "ses-live")
        ).mappings().one()
    assert turn["state"] == "active"
    assert session["agent_status"] == "running"


@pytest.mark.anyio
async def test_cancel_keeps_restored_native_runtime_on_interrupt_path(
    tmp_path: Path,
) -> None:
    """A restored native poll is live even when in_flight is empty."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-restored", backend="opencode")
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses-restored")
            .values(agent_status="running")
        )
        _delivery(conn, "delivery-restored", "ses-restored")
        delivery_store.insert_turn(
            conn,
            turn_id="trn-restored",
            session_id="ses-restored",
            initial_delivery_id="delivery-restored",
            state="active",
            backend="opencode",
            dispatch_text="restored",
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    manager._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        "opencode:native-restored:1",
    )
    seen = {}

    async def _deliver(request, *, context=None):
        seen["priority"] = request.priority
        seen["turn_id"] = request.expected_turn_id
        return SimpleNamespace(state="waiting_terminal", reason=None)

    manager.deliver = _deliver
    result = await manager.cancel("ses-restored")

    assert result == {
        "ok": True,
        "session_id": "ses-restored",
        "status": "cancel_requested",
    }
    assert seen == {"priority": "p0", "turn_id": "trn-restored"}
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, "trn-restored")
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == "ses-restored")
        ).mappings().one()
    assert turn["state"] == "active"
    assert session["agent_status"] == "running"


@pytest.mark.anyio
async def test_cancel_resumes_queued_successor_after_runtime_gone(
    tmp_path: Path,
) -> None:
    """Settling a leftover owner must start the next claimed successor."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-queue", backend="opencode")
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses-queue")
            .values(agent_status="running")
        )
        _delivery(conn, "delivery-owner", "ses-queue")
        delivery_store.insert_turn(
            conn,
            turn_id="trn-owner",
            session_id="ses-queue",
            initial_delivery_id="delivery-owner",
            state="active",
            backend="opencode",
            dispatch_text="owner",
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    started: list[str] = []
    resumed: list[str] = []

    def _terminalize(turn_id: str, *_args, **_kwargs):
        assert turn_id == "trn-owner"
        return {"changed": True, "successor_turn_id": "trn-successor"}

    async def _start(turn_id: str, **_kwargs) -> bool:
        started.append(turn_id)
        return True

    async def _resume(session_id: str) -> None:
        resumed.append(session_id)

    manager._terminalize_durable_turn = _terminalize
    manager._start_persisted_turn = _start
    manager._resume_post_terminal = _resume
    result = await manager.cancel("ses-queue")

    assert result["ok"] is True
    assert result["status"] == "stale_released"
    assert result["reason"] == "runtime_gone"
    assert started == ["trn-successor"]
    assert resumed == []


@pytest.mark.anyio
async def test_cancel_settles_unaccepted_starting_owner_when_runtime_is_gone(
    tmp_path: Path,
) -> None:
    """A leftover starting owner must settle as not_written, not hang in P0."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-starting", backend="opencode")
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses-starting")
            .values(agent_status="running")
        )
        _delivery(conn, "delivery-starting", "ses-starting")
        queued = delivery_store.get_delivery(conn, "delivery-starting")
        delivery_store.claim_start_batch(
            conn,
            turn_id="trn-starting",
            session_id="ses-starting",
            backend="opencode",
            deliveries=[queued],
            dispatch_text="starting",
            attempt_id="attempt-starting",
        )

    manager = SessionTurnManager(SimpleNamespace())
    manager._engine = engine
    result = await manager.cancel("ses-starting")

    assert result["ok"] is True
    assert result["status"] == "stale_released"
    assert result["reason"] == "runtime_gone"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, "trn-starting")
        delivery = delivery_store.get_delivery(conn, "delivery-starting")
        session = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == "ses-starting")
        ).mappings().one()
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "not_written"
    assert turn["settled_by"] == "stopped"
    assert turn["terminal_evidence_kind"] == "runtime_gone"
    assert delivery["state"] == "retired"
    assert session["agent_status"] != "running"
