from __future__ import annotations

import asyncio
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
from storage import session_deliveries as delivery_store
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, messages, metadata, session_deliveries, session_turns


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Controller:
    def __init__(self) -> None:
        self.command_handler = SimpleNamespace(handle_stop=AsyncMock(return_value=True))
        self.agent_service = SimpleNamespace(agents={}, _turn_gates={})
        self.statuses: list[tuple[str, str]] = []

    @staticmethod
    def _session_id_from_context(context: MessageContext) -> str | None:
        return str((context.platform_specific or {}).get("workbench_session_id") or "") or None

    @staticmethod
    def resolve_agent_for_context(_context: MessageContext) -> str:
        return "codex"

    def set_agent_status(self, session_id: str, status: str) -> None:
        self.statuses.append((session_id, status))


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
    now = "2026-07-31T00:00:00Z"
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
                created_at=now,
                updated_at=now,
                last_active_at=now,
                metadata_json="{}",
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
    starts: list[str] = []
    starts_lock = threading.Lock()

    async def fake_run(_session_id, _context_value, _text, **kwargs):
        with starts_lock:
            starts.append(str(kwargs.get("logical_turn_id") or ""))

    manager_a._run = fake_run
    manager_b._run = fake_run
    yield manager_a, manager_b, engine_a, engine_b, starts
    engine_a.dispose()
    engine_b.dispose()


async def _activate(manager: SessionTurnManager, *, text: str = "primary") -> tuple[str, MessageContext]:
    context = _context()
    admitted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
        context=context,
    )
    assert admitted.turn_id
    turn_id = admitted.turn_id
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = "runtime-token"
    manager._active_identity = lambda backend, session_id, logical_id: (
        logical_id,
        "native-t1",
    )
    manager.on_native_start(
        context,
        backend="codex",
        runtime_key="runtime-key",
        runtime_turn_id="runtime-token",
    )
    return turn_id, context


def _delivery_rows(engine) -> list[dict]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(session_deliveries).order_by(
                    session_deliveries.c.created_at,
                    session_deliveries.c.id,
                )
            ).mappings()
        ]


def test_turn_terminal_between_p1_observation_and_claim_has_one_owner(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-009"""
    manager, terminal_manager, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    observed = threading.Event()
    release = threading.Event()
    steer_calls: list[str] = []

    def blocked_identity(_backend, _session_id, logical_id):
        observed.set()
        assert release.wait(5)
        return logical_id, "native-t1"

    manager._active_identity = blocked_identity

    async def unexpected_steer(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

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

    rows = _delivery_rows(engine)
    follow_up = next(row for row in rows if row["message_id"] == outcome.message_id)
    assert outcome.state == "starting"
    assert follow_up["priority"] == "p3"
    assert steer_calls == []
    assert sum(row["message_id"] == outcome.message_id for row in rows) == 1


def test_two_empty_p1_requests_cas_the_same_fifo_head_only(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-010"""
    first, second, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    for manager in (first, first):
        asyncio.run(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p3", content=f"queued-{len(_delivery_rows(engine))}"),
                context=_context(),
            )
        )
    barrier = threading.Barrier(2)
    steer_calls: list[str] = []

    def identity(_backend, _session, logical_id):
        barrier.wait(timeout=5)
        assert logical_id == turn_id
        return logical_id, "native-t1"

    async def accepted(_backend, request):
        steer_calls.append(request.text)
        return steer_result(SteerOutcome.ACCEPTED)

    first._active_identity = identity
    second._active_identity = identity
    first._steer = accepted
    second._steer = accepted
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                asyncio.run,
                manager.deliver(
                    DeliveryRequest(session_id="ses_fsm", priority="p1"),
                    context=_context(),
                ),
            )
            for manager in (first, second)
        ]
        outcomes = [future.result(timeout=5) for future in futures]

    assert sorted(outcome.state for outcome in outcomes) == ["attached", "refused"]
    assert len(steer_calls) == 1
    rows = _delivery_rows(engine)
    queued = [row for row in rows if row["state"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["priority"] == "p3"


def test_lost_accepted_receipt_restarts_in_reconciliation_without_resteer(
    managers,
    monkeypatch,
) -> None:
    """Scenario: MESSAGE-DELIVERY-011"""
    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    first._active_identity = lambda _b, _s, logical: (logical, "native-t1")
    calls = 0

    async def accepted(_backend, _request):
        nonlocal calls
        calls += 1
        return steer_result(SteerOutcome.ACCEPTED)

    first._steer = accepted
    original = delivery_store.record_steer_receipt

    def lost_receipt(*args, **kwargs):
        raise OSError("simulated receipt fsync loss")

    monkeypatch.setattr(delivery_store, "record_steer_receipt", lost_receipt)
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="accepted once"),
            context=_context(),
        )
    )
    monkeypatch.setattr(delivery_store, "record_steer_receipt", original)
    restarted._active_identity = lambda _b, _s, logical: (logical, "native-t1")
    asyncio.run(restarted.recover_durable_delivery_state())

    row = next(row for row in _delivery_rows(engine) if row["id"] == outcome.delivery_id)
    assert calls == 1
    assert row["state"] == "reconciling"
    assert row["target_turn_id"] == turn_id


def test_definitive_refusal_racing_idle_drain_starts_same_message_once(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-012"""
    manager, drainer, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    manager._active_identity = lambda _b, _s, logical: (logical, "native-t1")
    adapter_called = threading.Event()
    release_adapter = threading.Event()

    async def refused(_backend, _request):
        adapter_called.set()
        await asyncio.to_thread(release_adapter.wait, 5)
        return steer_result(SteerOutcome.REFUSED, reason="not_steerable")

    manager._steer = refused

    async def race():
        delivery_task = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="fallback"),
                context=_context(),
            )
        )
        assert await asyncio.to_thread(adapter_called.wait, 5)
        assert await drainer.terminalize_turn(turn_id)
        drain_task = asyncio.create_task(drainer.drain_delivery_queue("ses_fsm"))
        release_adapter.set()
        return await delivery_task, await drain_task

    outcome, _ = asyncio.run(race())
    row = next(row for row in _delivery_rows(engine) if row["id"] == outcome.delivery_id)
    assert row["message_id"] == outcome.message_id
    assert row["priority"] == "p3"
    assert len([turn for turn in starts if turn != turn_id]) == 1


def test_p0_successor_persistence_failure_never_calls_interrupt(managers, monkeypatch) -> None:
    """Scenario: MESSAGE-DELIVERY-013"""
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        original = delivery_store.insert_turn

        def fail_successor(conn, **kwargs):
            if kwargs.get("state") == "pending":
                raise OSError("simulated successor persistence failure")
            return original(conn, **kwargs)

        monkeypatch.setattr(delivery_store, "insert_turn", fail_successor)
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
    with engine.connect() as conn:
        assert conn.execute(select(messages).where(messages.c.content_text == "replacement")).first() is None


def test_p0_terminal_and_restart_claim_successor_once_after_terminal(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-014"""
    manager, restarted, engine, _engine_b, starts = managers

    async def run():
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
        successor_id = next(
            row["successor_turn_id"]
            for row in _delivery_rows(engine)
            if row["id"] == admitted.delivery_id
        )
        assert successor_id not in starts
        await restarted.recover_durable_delivery_state()
        terminal_resume = restarted.on_native_terminal(context, outcome="terminal")
        assert terminal_resume is not None
        await terminal_resume
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return successor_id

    successor_id = asyncio.run(run())
    assert starts.count(successor_id) == 1
    assert manager.controller.command_handler.handle_stop.await_count == 1
    assert asyncio.run(restarted.terminalize_turn(successor_id))
    successor_delivery = next(
        row for row in _delivery_rows(engine) if row["successor_turn_id"] == successor_id
    )
    assert successor_delivery["state"] == "completed"


def test_two_idle_p3_admissions_leave_one_fifo_loser(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-015"""
    first, second, engine, _engine_b, starts = managers
    barrier = threading.Barrier(2)
    original_a = first._delivery_backend
    original_b = second._delivery_backend

    def synchronize(original):
        def wrapped(session_id, context):
            barrier.wait(timeout=5)
            return original(session_id, context)

        return wrapped

    first._delivery_backend = synchronize(original_a)
    second._delivery_backend = synchronize(original_b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                asyncio.run,
                manager.deliver(
                    DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
                    context=_context(),
                ),
            )
            for manager, text in ((first, "first"), (second, "second"))
        ]
        outcomes = [future.result(timeout=10) for future in futures]

    assert sorted(outcome.state for outcome in outcomes) == ["queued", "starting"]
    assert len(starts) == 1
    rows = _delivery_rows(engine)
    assert len([row for row in rows if row["state"] == "queued"]) == 1
    assert len([row for row in rows if row["state"] == "starting"]) == 1


def test_late_t1_terminal_cannot_clear_active_t2(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-016"""
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        t1, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(task=holder, context=context, logical_turn_id=t1)
        delivery = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="t2"),
            context=_context(),
        )
        await manager.terminalize_turn(t1)
        with engine.connect() as conn:
            owner = delivery_store.active_turn(conn, "ses_fsm")
        assert owner is not None
        t2 = str(owner["id"])
        t2_context = _context()
        t2_context.platform_specific["turn_token"] = t2
        manager._active_identity = lambda _b, _s, logical: (logical, "native-t2")
        manager.on_native_start(
            t2_context,
            backend="codex",
            runtime_key="runtime-t2",
            runtime_turn_id="token-t2",
        )
        assert not await manager.terminalize_turn(t1)
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return delivery, t2

    _delivery, t2 = asyncio.run(run())
    with engine.connect() as conn:
        owner = delivery_store.active_turn(conn, "ses_fsm")
    assert owner is not None
    assert owner["id"] == t2
    assert owner["state"] == "active"


def test_native_start_ambiguity_is_quarantined_without_duplicate_dispatch(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-017"""
    manager, restarted, engine, _engine_b, _starts = managers
    dispatch_calls = 0

    async def may_have_written(_session_id, _context_value, _text, **_kwargs):
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise ConnectionError("native start receipt lost")

    manager._run = may_have_written
    outcome = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="ambiguous"),
            context=_context(),
        )
    )
    asyncio.run(restarted.recover_durable_delivery_state())

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(outcome.turn_id))
    assert dispatch_calls == 1
    assert turn is not None
    assert turn["state"] == "quarantined"
    assert turn["start_attempt_id"]


def test_async_dispatch_failure_keeps_starting_delivery_quarantined(
    managers,
    monkeypatch,
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers
    entered = asyncio.Event()
    release = asyncio.Event()

    async def may_have_written(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise ConnectionError("native dispatch outcome lost")

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        may_have_written,
    )
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    manager.controller.emit_agent_message = AsyncMock()

    async def run():
        admitted = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="ambiguous after dispatch entered",
            ),
            context=_context(),
        )
        await entered.wait()
        holder = manager.in_flight["ses_fsm"].task
        release.set()
        await holder
        return admitted

    admitted = asyncio.run(run())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
        delivery = delivery_store.get_delivery(conn, str(admitted.delivery_id))
    assert turn is not None
    assert turn["state"] == "quarantined"
    assert delivery is not None
    assert delivery["state"] == "starting"
    manager.controller.emit_agent_message.assert_awaited_once()


def test_refused_native_start_requeues_without_automatic_redispatch(
    managers,
    monkeypatch,
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers
    release = asyncio.Event()

    async def refused(*_args, **_kwargs):
        await release.wait()
        return SimpleNamespace(settled_by="refused_concurrent_turn")

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        refused,
    )
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)

    async def run():
        admitted = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="retry after native owner releases",
            ),
            context=_context(),
        )
        holder = manager.in_flight["ses_fsm"].task
        release.set()
        await holder
        await asyncio.sleep(0)
        return admitted

    admitted = asyncio.run(run())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
        delivery = delivery_store.get_delivery(conn, str(admitted.delivery_id))
        owner = delivery_store.active_turn(conn, "ses_fsm")
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "refused_concurrent_turn"
    assert delivery is not None
    assert delivery["state"] == "queued"
    assert delivery["target_turn_id"] is None
    assert owner is None


def test_restart_reconciles_unrecorded_p0_interrupt_without_retry(managers) -> None:
    manager, restarted, engine, _engine_b, _starts = managers

    async def leave_interrupt_unrecorded(_session_id, _turn_id, _delivery_id):
        return {"state": "interrupting"}

    async def run():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        manager._interrupt_durable_turn = leave_interrupt_unrecorded
        delivery = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
            context=_context(),
        )
        assert delivery.state == "interrupting"
        await restarted.recover_durable_delivery_state()
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return delivery

    delivery = asyncio.run(run())
    row = next(item for item in _delivery_rows(engine) if item["id"] == delivery.delivery_id)
    assert row["state"] == "reconciling"
    restarted.controller.command_handler.handle_stop.assert_not_awaited()


def test_missing_runtime_owner_persists_p0_reconciliation(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    asyncio.run(_activate(manager))
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0"),
            context=_context(),
        )
    )
    row = next(item for item in _delivery_rows(engine) if item["id"] == result.delivery_id)
    assert result.state == "reconciling"
    assert row["state"] == "reconciling"
    assert row["receipt_outcome"] == "unknown"
    manager.controller.command_handler.handle_stop.assert_not_awaited()


def test_late_positive_native_evidence_rebinds_quarantined_turn(managers) -> None:
    manager, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    asyncio.run(restarted.recover_durable_delivery_state())
    with engine.connect() as conn:
        quarantined = delivery_store.get_turn(conn, turn_id)
    assert quarantined is not None
    assert quarantined["state"] == "quarantined"

    restarted._active_identity = lambda _backend, _session, logical: (
        logical,
        "native-t1",
    )
    restarted._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    result = asyncio.run(
        restarted.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="after restore"),
            context=_context(),
        )
    )

    assert result.state == "attached"
    with engine.connect() as conn:
        rebound = delivery_store.get_turn(conn, turn_id)
    assert rebound is not None
    assert rebound["state"] == "active"
    assert rebound["native_turn_id"] == "native-t1"
    restarted._steer.assert_awaited_once()


def test_p1_during_unbound_start_reconciles_without_steer_or_fallback(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="starting"),
            context=_context(),
        )
    )
    assert admitted.turn_id
    steer = AsyncMock()
    manager._steer = steer

    follow_up = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="do not duplicate"),
            context=_context(),
        )
    )

    row = next(item for item in _delivery_rows(engine) if item["id"] == follow_up.delivery_id)
    assert follow_up.state == "reconciling"
    assert row["priority"] == "p1"
    assert row["target_turn_id"] == admitted.turn_id
    assert row["receipt_outcome"] == "unknown"
    steer.assert_not_awaited()


def test_definitive_context_failure_requeues_without_quarantining(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    start = manager._start_persisted_turn

    async def hold_before_dispatch(_turn_id, *, context=None):
        return False

    manager._start_persisted_turn = hold_before_dispatch
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="retry later"),
            context=_context(),
        )
    )
    assert queued.state == "starting"
    assert queued.turn_id
    manager._start_persisted_turn = start

    def missing_context(_session_id):
        raise LookupError("session routing unavailable")

    manager._build_context = missing_context
    assert not asyncio.run(manager._start_persisted_turn(str(queued.turn_id)))

    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
        owner = delivery_store.active_turn(conn, "ses_fsm")
        prewrite_turn = conn.execute(
            select(session_turns)
            .where(session_turns.c.terminal_outcome == "pre_write_failure")
            .order_by(session_turns.c.created_at.desc())
        ).mappings().first()
    assert owner is None
    assert delivery is not None
    assert delivery["state"] == "queued"
    assert delivery["target_turn_id"] is None
    assert prewrite_turn is not None


def test_empty_p0_unknown_receipt_completes_only_on_exact_terminal_proof(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        queued = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="leave queued"),
            context=_context(),
        )
        assert queued.state == "queued"
        manager.controller.command_handler.handle_stop = AsyncMock(return_value=False)
        delivery = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0"),
            context=_context(),
        )
        assert delivery.state == "reconciling"
        before = next(row for row in _delivery_rows(engine) if row["id"] == delivery.delivery_id)
        assert before["message_id"] is None
        assert before["state"] == "reconciling"
        assert manager.on_native_terminal(context, outcome="terminal") is None
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return delivery, queued, turn_id

    delivery, queued, turn_id = asyncio.run(run())
    after = next(row for row in _delivery_rows(engine) if row["id"] == delivery.delivery_id)
    queued_after = next(row for row in _delivery_rows(engine) if row["id"] == queued.delivery_id)
    assert after["state"] == "completed"
    assert queued_after["state"] == "queued"
    assert starts == [turn_id]


def test_legacy_queue_drain_excludes_durable_owned_messages(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="durable only"),
            context=_context(),
        )
    )
    assert queued.state == "queued"
    assert asyncio.run(manager.terminalize_turn(turn_id))

    assert not asyncio.run(manager.flush_queue("ses_fsm"))
    row = next(item for item in _delivery_rows(engine) if item["id"] == queued.delivery_id)
    assert row["state"] == "queued"
    assert asyncio.run(manager.drain_delivery_queue("ses_fsm"))
    assert len(starts) == 2


def test_legacy_queue_drain_excludes_empty_p1_steering_owner(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="steer once"),
            context=_context(),
        )
    )
    manager._active_identity = lambda _backend, _session, logical: (
        logical,
        "native-t1",
    )

    async def unknown_after_legacy_probe(_backend, request):
        assert request.expected_logical_turn_id == turn_id
        assert not await manager.flush_queue("ses_fsm")
        return steer_result(SteerOutcome.UNKNOWN, reason="receipt unavailable")

    manager._steer = unknown_after_legacy_probe
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1"),
            context=_context(),
        )
    )
    assert result.delivery_id == queued.delivery_id
    row = next(item for item in _delivery_rows(engine) if item["id"] == queued.delivery_id)
    assert row["state"] == "reconciling"


def test_concurrent_p0_successors_claim_one_owner_and_retain_the_other(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        first = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="first replacement"),
            context=_context(),
        )
        second = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="second replacement"),
            context=_context(),
        )
        assert first.state == second.state == "waiting_terminal"
        assert await manager.terminalize_turn(turn_id)
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)

    asyncio.run(run())
    rows = _delivery_rows(engine)
    replacement_rows = [row for row in rows if row["priority"] == "p0"]
    assert sorted(row["state"] for row in replacement_rows) == ["queued", "starting"]
    with engine.connect() as conn:
        owner = delivery_store.active_turn(conn, "ses_fsm")
        pending = conn.execute(
            select(session_turns.c.id).where(session_turns.c.state == "pending")
        ).all()
    assert owner is not None
    assert starts.count(str(owner["id"])) == 1
    assert pending == []
