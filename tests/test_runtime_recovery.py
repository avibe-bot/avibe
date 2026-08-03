from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from core.runtime_recovery import (
    FallbackRequestRecoveryHandler,
    SessionDeliveryRecoveryHandler,
)
from core.runtime_work import RuntimeWorkLane, RuntimeWorkSupervisor
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


def _session(conn, session_id: str) -> None:
    conn.execute(
        agent_sessions.insert().values(
            id=session_id,
            scope_id=None,
            agent_id=None,
            agent_name="codex",
            agent_backend="codex",
            agent_variant="codex",
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
            queue_hold_state="open",
            queue_hold_version=1,
            queue_held_at=None,
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
        snapshot={"content_json": json.dumps({"text": delivery_id})},
        dispatch_text=delivery_id,
        turn_id=None,
    )


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
        supervisor.register(
            RuntimeWorkLane.REQUESTS,
            FallbackRequestRecoveryHandler(store),
        )
        await supervisor.activate()
        for _ in range(50):
            with store.engine.connect() as conn:
                status = conn.execute(
                    select(agent_runs.c.status).where(
                        agent_runs.c.id == "run-pre-execution"
                    )
                ).scalar_one()
            if status == "queued":
                break
            await asyncio.sleep(0.01)
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
