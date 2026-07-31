from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from core.services.agent_steering import SteerOutcome, result as steer_result
from core.session_turns import DeliveryRequest, SessionTurnManager, Turn
from modules.im import MessageContext
from storage import message_deliveries as delivery_store
from storage import messages_service
from storage import workbench_sessions_service
from storage.db import create_sqlite_engine
from storage.models import (
    agent_runs,
    agent_sessions,
    message_deliveries,
    messages,
    metadata,
    session_turns,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Controller:
    def __init__(self) -> None:
        self.command_handler = SimpleNamespace(handle_stop=AsyncMock(return_value=True))
        self.agent_service = SimpleNamespace(agents={}, _turn_gates={})
        self.config = SimpleNamespace(language="en")
        self.statuses: list[tuple[str, str]] = []

    @staticmethod
    def _session_id_from_context(context: MessageContext) -> str | None:
        return str((context.platform_specific or {}).get("workbench_session_id") or "") or None

    @staticmethod
    def resolve_agent_for_context(_context: MessageContext) -> str:
        return "codex"

    def set_agent_status(self, session_id: str, status: str) -> None:
        self.statuses.append((session_id, status))

    @staticmethod
    def _get_session_key(context: MessageContext) -> str:
        return f"avibe::{(context.platform_specific or {}).get('agent_session_id')}"


def _context(session_id: str = "ses_fsm") -> MessageContext:
    return MessageContext(
        user_id="user",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "workbench_session_id": session_id,
            "agent_session_id": session_id,
            "agent_session_target": {
                "id": session_id,
                "agent_backend": "codex",
            },
        },
    )


def _seed_session(engine, session_id: str = "ses_fsm") -> None:
    now = "2026-08-01T00:00:00+00:00"
    with engine.begin() as conn:
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
                session_anchor=session_id,
                workdir="/tmp",
                native_session_id="",
                title=None,
                status="active",
                visibility="foreground",
                pinned=0,
                agent_status="idle",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )


@pytest.fixture
def managers(tmp_path: Path):
    db_path = tmp_path / "fsm.sqlite"
    engine_a = create_sqlite_engine(db_path)
    engine_b = create_sqlite_engine(db_path)
    metadata.create_all(engine_a)
    _seed_session(engine_a)

    controller_a = _Controller()
    controller_b = _Controller()
    manager_a = SessionTurnManager(controller_a, build_context=_context)
    manager_b = SessionTurnManager(controller_b, build_context=_context)
    manager_a._engine = engine_a
    manager_b._engine = engine_b
    starts: list[tuple[str, str]] = []
    starts_lock = threading.Lock()

    async def fake_run(_session_id, _context_value, text, **kwargs):
        with starts_lock:
            starts.append((str(kwargs.get("logical_turn_id") or ""), text))

    manager_a._run = fake_run
    manager_b._run = fake_run
    yield manager_a, manager_b, engine_a, engine_b, starts
    engine_a.dispose()
    engine_b.dispose()


async def _activate(
    manager: SessionTurnManager,
    *,
    text: str = "primary",
) -> tuple[str, MessageContext]:
    context = _context()
    admitted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
        context=context,
    )
    assert admitted.turn_id
    turn_id = admitted.turn_id
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    manager._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    manager.on_native_start(
        context,
        backend="codex",
        runtime_key=f"runtime-key-{turn_id}",
        runtime_turn_id=f"runtime-{turn_id}",
    )
    return turn_id, context


def _rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(message_deliveries).order_by(
                    message_deliveries.c.submitted_at,
                    message_deliveries.c.id,
                )
            ).mappings()
        ]


def _row(engine, delivery_id: str) -> dict:
    with engine.connect() as conn:
        row = delivery_store.get_delivery(conn, delivery_id)
    assert row is not None
    return row


def test_terminal_between_p1_observation_and_claim_starts_same_submission_once(managers) -> None:
    """MESSAGE-DELIVERY-009: a stale active observation cannot steer or duplicate."""

    manager, terminal_manager, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    observed = threading.Event()
    release = threading.Event()
    steer_calls: list[str] = []

    def blocked_identity(_backend, _session_id, logical_id):
        observed.set()
        assert release.wait(5)
        return logical_id, f"native-{logical_id}"

    async def unexpected_steer(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

    manager._active_identity = blocked_identity
    manager._steer = unexpected_steer
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            asyncio.run,
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="follow-up"),
                context=_context(),
            ),
        )
        assert observed.wait(5)
        assert asyncio.run(terminal_manager.terminalize_turn(turn_id))
        release.set()
        outcome = future.result(timeout=5)

    delivery = _row(engine, str(outcome.delivery_id))
    assert outcome.state == "start_attempting"
    assert delivery["current_target_turn_id"] == outcome.turn_id
    assert steer_calls == []
    assert [text for started_turn, text in starts if started_turn == outcome.turn_id] == [
        "follow-up"
    ]
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id).where(messages.c.id == outcome.delivery_id)).first() is None


def test_p1_stale_observed_turn_does_not_retarget_replacement(managers) -> None:
    manager, replacement_manager, engine, _engine_b, starts = managers
    old_turn_id, _ = asyncio.run(_activate(manager))
    observed = threading.Event()
    release = threading.Event()
    steer_calls: list[tuple[str, str]] = []

    def blocked_identity(_backend, _session_id, logical_id):
        observed.set()
        assert release.wait(5)
        return logical_id, f"native-{logical_id}"

    async def unexpected_steer(_backend, request):
        steer_calls.append((request.expected_logical_turn_id, request.text))
        return steer_result(SteerOutcome.ACCEPTED)

    manager._active_identity = blocked_identity
    manager._steer = unexpected_steer
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            asyncio.run,
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="stale-p1"),
                context=_context(),
            ),
        )
        assert observed.wait(5)
        assert asyncio.run(replacement_manager.terminalize_turn(old_turn_id))
        replacement_turn_id, _ = asyncio.run(
            _activate(replacement_manager, text="replacement-owner")
        )
        release.set()
        outcome = future.result(timeout=5)

    delivery = _row(engine, str(outcome.delivery_id))
    assert outcome.state == "queued"
    assert delivery["current_target_turn_id"] is None
    assert steer_calls == []
    assert [text for turn_id, text in starts if turn_id == replacement_turn_id] == [
        "replacement-owner"
    ]
    assert [text for _, text in starts].count("stale-p1") == 0


def test_two_empty_p1_requests_claim_exact_same_head_only(managers) -> None:
    """MESSAGE-DELIVERY-010: the losing head CAS cannot select head two."""

    first, second, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    for text in ("head-one", "head-two"):
        asyncio.run(
            first.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
                context=_context(),
            )
        )
    barrier = threading.Barrier(2)
    steer_calls: list[str] = []

    def identity(_backend, _session, logical_id):
        barrier.wait(timeout=5)
        return logical_id, f"native-{logical_id}"

    async def accepted(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

    for manager in (first, second):
        manager._active_identity = identity
        manager._steer = accepted
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=5)
            for future in [
                pool.submit(
                    asyncio.run,
                    manager.deliver(
                        DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
                        context=_context(),
                    ),
                )
                for manager in (first, second)
            ]
        ]

    assert sorted(result.state for result in results) == ["accepted", "refused"]
    assert steer_calls == ["head-one"]
    queued = [row for row in _rows(engine) if row["state"] == "queued"]
    assert [row["dispatch_text"] for row in queued] == ["head-two"]
    assert _row(engine, str(results[0].delivery_id))["accepted_turn_id"] in {None, turn_id}


def test_empty_p1_refuses_when_exact_turn_changes_before_head_claim(managers) -> None:
    manager, replacement_manager, engine, _engine_b, _starts = managers
    old_turn_id, _ = asyncio.run(_activate(manager))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="exact-head"),
            context=_context(),
        )
    )
    observed = threading.Event()
    release = threading.Event()
    steer_calls: list[str] = []

    def blocked_identity(_backend, _session_id, logical_id):
        observed.set()
        assert release.wait(5)
        return logical_id, f"native-{logical_id}"

    async def unexpected_steer(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

    manager._active_identity = blocked_identity
    manager._steer = unexpected_steer
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            asyncio.run,
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
                context=_context(),
            ),
        )
        assert observed.wait(5)
        assert asyncio.run(replacement_manager.terminalize_turn(old_turn_id))
        asyncio.run(_activate(replacement_manager, text="replacement-owner"))
        release.set()
        outcome = future.result(timeout=5)

    assert outcome.state == "refused"
    assert outcome.reason == "stale_turn"
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"
    assert steer_calls == []


def test_p1_steer_uses_persisted_dispatch_text(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p1",
            state="reserved",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="display content",
            ),
            dispatch_text="canonical agent prompt",
        )
    manager._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    steer_calls: list[str] = []

    async def accepted(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

    manager._steer = accepted
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="noncanonical retry payload",
                delivery_id=delivery_id,
            ),
            context=_context(),
        )
    )

    assert result.state == "accepted"
    assert steer_calls == ["canonical agent prompt"]
    row = _row(engine, delivery_id)
    assert row["accepted_turn_id"] == turn_id
    with engine.connect() as conn:
        materialized = conn.execute(
            select(messages.c.content_text).where(messages.c.id == delivery_id)
        ).scalar_one()
    assert materialized == "display content"


def test_lost_accepted_receipt_restarts_unknown_without_resteer(
    managers,
    monkeypatch,
) -> None:
    """MESSAGE-DELIVERY-011: adapter acceptance is may-have-written after DB loss."""

    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    first._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    calls = 0

    async def accepted(_backend, _request):
        nonlocal calls
        calls += 1
        return steer_result(SteerOutcome.ACCEPTED)

    first._steer = accepted
    original = delivery_store.materialize_acceptance

    def lose_receipt(*args, **kwargs):
        raise OSError("simulated receipt fsync loss")

    monkeypatch.setattr(delivery_store, "materialize_acceptance", lose_receipt)
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="accepted once"),
            context=_context(),
        )
    )
    monkeypatch.setattr(delivery_store, "materialize_acceptance", original)
    restarted._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(outcome.delivery_id))
    assert calls == 1
    assert row["state"] == "reconciling_steer"
    assert row["current_target_turn_id"] == turn_id
    assert row["current_receipt_outcome"] == "unknown"
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id).where(messages.c.id == row["id"])).first() is None


def test_definitive_refusal_racing_idle_drain_starts_same_delivery_once(managers) -> None:
    """MESSAGE-DELIVERY-012: refusal fallback and drain share one writer claim."""

    manager, drainer, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    manager._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    adapter_called = threading.Event()
    release_adapter = threading.Event()

    async def refused(_backend, _request):
        adapter_called.set()
        await asyncio.to_thread(release_adapter.wait, 5)
        return steer_result(SteerOutcome.REFUSED, reason="not_steerable")

    manager._steer = refused

    async def race():
        task = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="fallback"),
                context=_context(),
            )
        )
        assert await asyncio.to_thread(adapter_called.wait, 5)
        assert await drainer.terminalize_turn(turn_id)
        concurrent_drain = asyncio.create_task(drainer.drain_delivery_queue("ses_fsm"))
        release_adapter.set()
        return await task, await concurrent_drain

    outcome, _ = asyncio.run(race())
    row = _row(engine, str(outcome.delivery_id))
    assert row["state"] == "start_attempting"
    matching_starts = [item for item in starts if item[1] == "fallback"]
    assert len(matching_starts) == 1
    assert matching_starts[0][0] == row["current_target_turn_id"]


def test_p0_successor_persistence_failure_never_calls_stop(managers, monkeypatch) -> None:
    """MESSAGE-DELIVERY-013: successor Delivery and Turn commit before Stop."""

    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        original = delivery_store.insert_turn

        def fail_waiting(conn, **kwargs):
            if kwargs.get("state") == "waiting":
                raise OSError("simulated successor persistence failure")
            return original(conn, **kwargs)

        monkeypatch.setattr(delivery_store, "insert_turn", fail_waiting)
        try:
            with pytest.raises(OSError, match="successor persistence"):
                await manager.deliver(
                    DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
                    context=_context(),
                )
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    asyncio.run(run())
    assert manager.controller.command_handler.handle_stop.await_count == 0
    assert all(row["dispatch_text"] != "replacement" for row in _rows(engine))


def test_definitive_p0_refusal_restores_preexisting_queue_hold(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        with engine.begin() as conn:
            assert delivery_store.set_queue_hold(conn, "ses_fsm", held=True)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )

        async def refused(stop_context):
            stop_context.platform_specific["stop_failure_reason"] = "refused"
            return False

        manager.controller.command_handler.handle_stop = AsyncMock(side_effect=refused)
        try:
            result = await manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p0", content=None),
                context=context,
            )
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
        assert result.state == "refused"

    asyncio.run(run())
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm") is True


def test_p0_terminal_restart_claims_successor_once_after_old_terminal(managers) -> None:
    """MESSAGE-DELIVERY-014: control, terminal, and restart converge once."""

    manager, restarted, engine, _engine_b, starts = managers

    async def run() -> tuple[str, str]:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="successor"),
            context=_context(),
        )
        assert admitted.state == "waiting_terminal"
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        successor_id = str(old["control_successor_turn_id"])
        assert successor_id not in [started for started, _ in starts]
        await restarted.recover_durable_delivery_state()
        terminal_resume = restarted.on_native_terminal(context, outcome="completed")
        assert terminal_resume is not None
        await terminal_resume
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return successor_id, str(admitted.delivery_id)

    successor_id, delivery_id = asyncio.run(run())
    assert [turn for turn, text in starts if text == "successor"] == [successor_id]
    assert manager.controller.command_handler.handle_stop.await_count == 1
    delivery = _row(engine, delivery_id)
    assert delivery["state"] == "start_attempting"
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm") is True


def test_two_idle_p3_admissions_leave_one_fifo_loser(managers) -> None:
    """MESSAGE-DELIVERY-015: two connections cannot own two live Turns."""

    first, second, engine, _engine_b, starts = managers
    barrier = threading.Barrier(2)
    original_a = first._delivery_backend
    original_b = second._delivery_backend

    def synchronized(original):
        def wrapped(session_id, context):
            barrier.wait(timeout=5)
            return original(session_id, context)

        return wrapped

    first._delivery_backend = synchronized(original_a)
    second._delivery_backend = synchronized(original_b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=5)
            for future in [
                pool.submit(
                    asyncio.run,
                    manager.deliver(
                        DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
                        context=_context(),
                    ),
                )
                for manager, text in ((first, "first"), (second, "second"))
            ]
        ]

    assert sorted(result.state for result in outcomes) == ["queued", "start_attempting"]
    assert len(starts) == 1
    with engine.connect() as conn:
        live = conn.execute(
            select(session_turns.c.id).where(session_turns.c.state.in_(("starting", "active")))
        ).all()
    assert len(live) == 1


def test_late_t1_terminal_cannot_clear_active_t2(managers) -> None:
    """MESSAGE-DELIVERY-016: terminal identity is exact and versioned."""

    manager, other, engine, _engine_b, _starts = managers
    t1, context1 = asyncio.run(_activate(manager, text="t1"))
    asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="t2"),
            context=_context(),
        )
    )
    assert asyncio.run(other.terminalize_turn(t1))
    assert asyncio.run(other.drain_delivery_queue("ses_fsm"))
    with engine.connect() as conn:
        t2 = delivery_store.active_turn(conn, "ses_fsm")
    assert t2 is not None and t2["id"] != t1
    context2 = _context()
    context2.platform_specific.update(
        {
            "turn_token": t2["id"],
            "agent_runtime_turn_token": f"runtime-{t2['id']}",
        }
    )
    other._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    other.on_native_start(
        context2,
        backend="codex",
        runtime_key=f"key-{t2['id']}",
        runtime_turn_id=f"runtime-{t2['id']}",
    )
    assert other.on_native_terminal(context1, outcome="completed") is None
    with engine.connect() as conn:
        still_active = delivery_store.active_turn(conn, "ses_fsm")
    assert still_active is not None and still_active["id"] == t2["id"]


def test_start_write_ambiguity_survives_restart_without_duplicate_dispatch(managers) -> None:
    """MESSAGE-DELIVERY-017: native start is may-have-written."""

    first, restarted, engine, _engine_b, starts = managers

    async def ambiguous(_session_id, _context_value, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))
        raise OSError("connection lost after native write")

    first._run = ambiguous
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="once"),
            context=_context(),
        )
    )
    restarted._active_identity = lambda *_args: None
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(outcome.delivery_id))
    assert row["state"] == "reconciling_start"
    assert [text for _, text in starts] == ["once"]
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id)).all() == []


def test_terminal_agent_run_wins_after_start_claim_before_native_dispatch(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    attempt_id = delivery_store.new_attempt_id()
    run_id = "run-canceled-after-start-claim"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                definition_id=None,
                run_type="agent",
                status="canceled",
                cancel_requested=1,
                session_id="ses_fsm",
                created_at=now,
                completed_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="start_attempting",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="must not run",
                native_message_id=f"agent_run:{run_id}",
            ),
            dispatch_text="must not run",
            current_attempt_id=attempt_id,
            current_attempt_kind="start",
            current_target_turn_id=turn_id,
        )
        delivery_store.insert_turn(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            initial_delivery_id=delivery_id,
            state="starting",
            backend="codex",
        )

    assert asyncio.run(manager._start_persisted_turn(turn_id, context=_context())) is False
    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None and turn["terminal_outcome"] == "not_written"


def test_materialized_message_uses_the_delivery_timestamp_format(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    _turn_id, _context_value = asyncio.run(_activate(manager, text="ordered input"))
    with engine.connect() as conn:
        delivery = conn.execute(
            select(message_deliveries).where(
                message_deliveries.c.dispatch_text == "ordered input"
            )
        ).mappings().one()
        message = conn.execute(
            select(messages).where(messages.c.id == delivery["id"])
        ).mappings().one()
    assert message["created_at"] == delivery["submitted_at"]
    assert message["created_at"].endswith("Z")
    assert "." not in message["created_at"]


def test_two_restart_recoveries_claim_reserved_submission_once(managers) -> None:
    first, second, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="reserved",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="recover once",
            ),
            dispatch_text="recover once",
        )
    barrier = threading.Barrier(2)
    original_first = first._delivery_backend
    original_second = second._delivery_backend

    def synchronized(original):
        def wrapped(session_id, context):
            barrier.wait(timeout=5)
            return original(session_id, context)

        return wrapped

    first._delivery_backend = synchronized(original_first)
    second._delivery_backend = synchronized(original_second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        _results = [
            future.result(timeout=5)
            for future in (
                pool.submit(asyncio.run, first.recover_durable_delivery_state()),
                pool.submit(asyncio.run, second.recover_durable_delivery_state()),
            )
        ]

    assert [text for _, text in starts] == ["recover once"]
    assert _row(engine, delivery_id)["state"] == "start_attempting"


def test_reserved_attachment_only_submission_recovers_exact_dispatch_inputs(
    managers,
    tmp_path: Path,
) -> None:
    from storage import media_service

    manager, _other, engine, _engine_b, _starts = managers
    attachment = tmp_path / "input.txt"
    attachment.write_text("durable attachment", encoding="utf-8")
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            kind="file",
            source="user_upload",
            local_path=str(attachment),
            file_name="input.txt",
            content_type="text/plain",
        )
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="reserved",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="",
                content={"attachments": [{"token": token}]},
            ),
            dispatch_text="",
        )
    dispatched: list[tuple[str, list[str]]] = []

    async def capture(_session_id, context, text, **_kwargs):
        dispatched.append(
            (
                text,
                [str(item.local_path) for item in (context.files or [])],
            )
        )

    manager._run = capture
    asyncio.run(manager.recover_durable_delivery_state())

    assert dispatched == [("", [str(attachment)])]
    assert _row(engine, delivery_id)["state"] == "start_attempting"


def test_unresolved_p1_fence_blocks_later_but_not_older_fifo(managers) -> None:
    """MESSAGE-DELIVERY-101: fallback-capable unknown preserves submission order."""

    manager, other, engine, _engine_b, starts = managers
    t1, _ = asyncio.run(_activate(manager))
    asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="older"),
            context=_context(),
        )
    )
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.UNKNOWN))
    unknown = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="unknown"),
            context=_context(),
        )
    )
    asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="later"),
            context=_context(),
        )
    )
    assert asyncio.run(other.terminalize_turn(t1))
    assert asyncio.run(other.drain_delivery_queue("ses_fsm"))
    older_turn = _row(engine, next(row["id"] for row in _rows(engine) if row["dispatch_text"] == "older"))[
        "current_target_turn_id"
    ]
    assert older_turn
    assert asyncio.run(other.terminalize_turn(str(older_turn)))
    assert asyncio.run(other.drain_delivery_queue("ses_fsm")) is False

    assert _row(engine, str(unknown.delivery_id))["state"] == "reconciling_steer"
    assert [text for _, text in starts].count("older") == 1
    assert [text for _, text in starts].count("later") == 0


def test_held_old_backlog_does_not_block_new_idle_p3(managers) -> None:
    """MESSAGE-DELIVERY-102: hold blocks autonomous backlog only."""

    manager, other, engine, _engine_b, starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="held-old"),
            context=_context(),
        )
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        stopped = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content=None),
            context=context,
        )
        assert stopped.state == "waiting_terminal"
        assert await other.terminalize_turn(turn_id)
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        admitted = await other.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="new-idle"),
            context=_context(),
        )
        assert admitted.state == "start_attempting"

    asyncio.run(run())
    rows = _rows(engine)
    assert next(row for row in rows if row["dispatch_text"] == "held-old")["state"] == "queued"
    assert [text for _, text in starts].count("new-idle") == 1
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm") is True


def test_accepted_steer_after_target_terminal_attaches_exact_terminal_turn(managers) -> None:
    manager, terminal_manager, engine, _engine_b, _starts = managers

    async def run():
        turn_id, _ = await _activate(manager)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def accepted(_backend, _request):
            entered.set()
            await release.wait()
            return steer_result(SteerOutcome.ACCEPTED)

        manager._steer = accepted
        pending = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="late receipt"),
                context=_context(),
            )
        )
        await entered.wait()
        assert await terminal_manager.terminalize_turn(turn_id)
        release.set()
        return turn_id, await pending

    turn_id, result = asyncio.run(run())
    row = _row(engine, str(result.delivery_id))
    assert result.state == "accepted"
    assert row["accepted_turn_id"] == turn_id
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        message = conn.execute(select(messages).where(messages.c.id == row["id"])).mappings().one()
    assert turn["state"] == "terminal"
    assert message["content_text"] == "late receipt"


def test_repeated_refusal_then_acceptance_preserves_attempt_history(managers) -> None:
    manager, other, engine, _engine_b, _starts = managers
    t1, _ = asyncio.run(_activate(manager, text="t1"))
    manager._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.REFUSED, reason="not_steerable")
    )
    first_attempt = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="retry me"),
            context=_context(),
        )
    )
    assert _row(engine, str(first_attempt.delivery_id))["state"] == "queued"
    assert asyncio.run(other.terminalize_turn(t1))
    t2, context2 = asyncio.run(_activate(other, text="new priority work"))
    other._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    promoted = asyncio.run(
        other.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
            context=context2,
        )
    )
    assert promoted.delivery_id == first_attempt.delivery_id
    row = _row(engine, str(promoted.delivery_id))
    history = json.loads(row["delivery_history_json"])
    steer_events = [event for event in history["events"] if event["kind"] == "steer"]
    assert [event["outcome"] for event in steer_events if event["outcome"] != "opened"] == [
        "refused",
        "accepted",
    ]
    assert row["accepted_turn_id"] == t2
    assert row["current_attempt_id"] is None


def test_archive_keeps_unknown_and_materializes_late_positive_evidence(managers) -> None:
    first, restarted, engine, _engine_b, starts = managers

    async def ambiguous(_session_id, _context_value, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))
        raise OSError("unknown native start")

    first._run = ambiguous
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="may be written"),
            context=_context(),
        )
    )
    with engine.begin() as conn:
        workbench_sessions_service.archive_session(conn, "ses_fsm")
    assert _row(engine, str(admitted.delivery_id))["state"] == "reconciling_start"
    restarted._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(admitted.delivery_id))
    assert row["state"] == "accepted"
    with engine.connect() as conn:
        message = conn.execute(select(messages).where(messages.c.id == row["id"])).mappings().one()
        session_status = conn.execute(
            select(agent_sessions.c.status, agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).one()
    assert message["content_text"] == "may be written"
    assert session_status == ("archived", "idle")
    assert [text for _, text in starts] == ["may be written"]


def test_definitive_steer_refusal_after_archive_retires_without_start(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, _ = await _activate(manager)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def refused(_backend, _request):
            entered.set()
            await release.wait()
            return steer_result(SteerOutcome.REFUSED, reason="not_steerable")

        manager._steer = refused
        pending = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="archived"),
                context=_context(),
            )
        )
        await entered.wait()
        with engine.begin() as conn:
            workbench_sessions_service.archive_session(conn, "ses_fsm")
        release.set()
        return turn_id, await pending

    turn_id, result = asyncio.run(run())
    row = _row(engine, str(result.delivery_id))
    assert result.state == "retired"
    assert row["state"] == "retired"
    assert row["current_attempt_id"] is None
    assert [started for started, _ in starts] == [turn_id]
    with engine.connect() as conn:
        assert conn.execute(
            select(messages.c.id).where(messages.c.id == result.delivery_id)
        ).first() is None


def test_archive_retires_unstarted_successor_without_creating_message(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> str:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="unstarted"),
            context=_context(),
        )
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return str(admitted.delivery_id)

    delivery_id = asyncio.run(run())
    with engine.begin() as conn:
        workbench_sessions_service.archive_session(conn, "ses_fsm")
    row = _row(engine, delivery_id)
    with engine.connect() as conn:
        successor = conn.execute(
            select(session_turns).where(session_turns.c.initial_delivery_id == delivery_id)
        ).mappings().one()
        message = conn.execute(select(messages.c.id).where(messages.c.id == delivery_id)).first()
    assert row["state"] == "retired"
    assert successor["terminal_outcome"] == "not_written"
    assert message is None


@pytest.mark.anyio
async def test_terminal_commit_publishes_replyless_inbox_settlement(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            messages.insert().values(
                id="msg_prior_result",
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="agent",
                type="result",
                source="agent",
                content_text="prior answer",
                content_json="{}",
                metadata_json="{}",
                created_at="2026-07-31T23:59:00Z",
                updated_at="2026-07-31T23:59:00Z",
            )
        )
    turn_id, context = await _activate(manager, text="silent request")
    done = asyncio.Event()
    manager.register_turn_sink(
        manager.controller._get_session_key(context),
        on_chunk=manager._noop_chunk,
        done_event=done,
        turn_token=turn_id,
        context=context,
    )
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.inbox_events.bus.publish",
        lambda event, payload: published.append((event, payload)),
    )

    manager.on_terminal_result(context, is_error=False)
    done.set()
    for _ in range(4):
        await asyncio.sleep(0)

    updates = [payload for event, payload in published if event == "inbox.session.updated"]
    assert updates
    assert updates[-1]["session_id"] == "ses_fsm"
    assert updates[-1]["replied"] is False
    with engine.connect() as conn:
        assert delivery_store.get_turn(conn, turn_id)["state"] == "terminal"


@pytest.mark.anyio
async def test_agent_initiated_continuation_materializes_in_configured_language(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager.controller.config.language = "zh"
    context = _context()

    assert manager.register_agent_initiated_turn(context) is True
    with engine.connect() as conn:
        row = messages_service.get_message(conn, str(context.message_id))
    assert row is not None
    assert row["text"] == "Agent 主动发起的续接"

    sink = manager.get_turn_sink(manager.controller._get_session_key(context))
    assert sink is not None
    sink["done_event"].set()
    for _ in range(4):
        await asyncio.sleep(0)
