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
from core.session_turns import DeliveryRequest, DeliveryResult, SessionTurnManager, Turn
from modules.im import MessageContext
from storage import messages_service
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


def test_terminal_race_fallback_preserves_older_legacy_fifo(managers) -> None:
    manager, terminal_manager, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    with engine.begin() as conn:
        messages_service.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="older legacy row",
        )
    observed = threading.Event()
    release = threading.Event()
    dispatched: list[str] = []

    def blocked_identity(_backend, _session_id, logical_id):
        observed.set()
        assert release.wait(5)
        return logical_id, "native-t1"

    async def capture_run(_session_id, _context_value, text, **_kwargs):
        dispatched.append(text)

    manager._active_identity = blocked_identity
    manager._run = capture_run
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            asyncio.run,
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p1",
                    content="fallback",
                ),
                context=_context(),
            ),
        )
        assert observed.wait(5)
        assert asyncio.run(terminal_manager.terminalize_turn(turn_id))
        release.set()
        outcome = future.result(timeout=5)

    with engine.connect() as conn:
        fallback = delivery_store.get_delivery(conn, str(outcome.delivery_id))
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert dispatched == ["older legacy row"]
    assert outcome.state == "queued"
    assert fallback is not None
    assert fallback["state"] == "queued"
    assert [row["id"] for row in queued] == [outcome.message_id]


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
                    DeliveryRequest(
                        session_id="ses_fsm",
                        priority="p1",
                        content="",
                    ),
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


def test_adapter_error_persists_reconciliation_without_retry(managers) -> None:
    manager, restarted, engine, _engine_b, _starts = managers
    asyncio.run(_activate(manager))
    manager._steer = AsyncMock(side_effect=ConnectionError("receipt unavailable"))

    outcome = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="do not retry unknown steer",
            ),
            context=_context(),
        )
    )
    asyncio.run(restarted.recover_durable_delivery_state())

    row = next(
        item
        for item in _delivery_rows(engine)
        if item["id"] == outcome.delivery_id
    )
    assert outcome.state == "reconciling"
    assert row["state"] == "reconciling"
    assert row["receipt_outcome"] == "unknown"
    manager._steer.assert_awaited_once()


def test_accepted_steer_after_target_terminal_completes_attachment(managers) -> None:
    manager, terminal_manager, engine, _engine_b, _starts = managers

    async def run():
        turn_id, _ = await _activate(manager)
        manager._active_identity = lambda _backend, _session, logical: (
            logical,
            "native-t1",
        )
        adapter_entered = asyncio.Event()
        release_adapter = asyncio.Event()

        async def accepted(_backend, _request):
            adapter_entered.set()
            await release_adapter.wait()
            return steer_result(SteerOutcome.ACCEPTED)

        manager._steer = accepted
        pending = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p1",
                    content="accepted before terminal",
                ),
                context=_context(),
            )
        )
        await adapter_entered.wait()
        assert await terminal_manager.terminalize_turn(turn_id)
        release_adapter.set()
        return turn_id, await pending

    turn_id, result = asyncio.run(run())
    row = next(
        item
        for item in _delivery_rows(engine)
        if item["id"] == result.delivery_id
    )
    assert result.state == "completed"
    assert result.turn_id == turn_id
    assert row["state"] == "completed"
    assert row["receipt_outcome"] == "accepted"


def test_p1_steers_through_persisted_turn_backend(managers) -> None:
    manager, _restarted, _engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    observed_backends: list[str] = []

    def active_identity(backend, _session, logical):
        observed_backends.append(backend)
        return logical, "native-t1"

    manager._active_identity = active_identity
    manager._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )
    drifted_context = _context()
    drifted_context.platform_specific["agent_session_target"][
        "agent_backend"
    ] = "claude"

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="target the durable owner",
            ),
            context=drifted_context,
        )
    )

    assert result.state == "attached"
    assert result.turn_id == turn_id
    assert observed_backends == ["codex"]
    assert manager._steer.await_args.args[0] == "codex"


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


def test_definitive_refusal_preserves_older_legacy_fifo(managers) -> None:
    manager, terminal_manager, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    with engine.begin() as conn:
        messages_service.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="older legacy row",
        )
    dispatched: list[str] = []

    async def capture_run(_session_id, _context_value, text, **_kwargs):
        dispatched.append(text)

    async def refused_after_terminal(_backend, _request):
        assert await terminal_manager.terminalize_turn(turn_id)
        return steer_result(SteerOutcome.REFUSED, reason="not_steerable")

    manager._run = capture_run
    manager._steer = refused_after_terminal
    outcome = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="fallback",
            ),
            context=_context(),
        )
    )

    with engine.connect() as conn:
        fallback = delivery_store.get_delivery(conn, str(outcome.delivery_id))
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert dispatched == ["older legacy row"]
    assert outcome.state == "queued"
    assert fallback is not None
    assert fallback["state"] == "queued"
    assert [row["id"] for row in queued] == [outcome.message_id]


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

    restarted._active_identity = lambda _backend, _session, logical: (
        logical,
        "native-restored",
    )
    restarted._steer = AsyncMock(
        return_value=steer_result(SteerOutcome.ACCEPTED)
    )
    attached = asyncio.run(
        restarted.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="attach after native evidence",
            ),
            context=_context(),
        )
    )
    assert attached.state == "attached"
    with engine.connect() as conn:
        rebound = delivery_store.get_turn(conn, str(outcome.turn_id))
    assert rebound is not None
    assert rebound["runtime_turn_id"] is None

    async def terminalize_restored_native() -> None:
        terminal_context = _context()
        terminal_context.platform_specific["turn_token"] = str(outcome.turn_id)
        terminal_context.platform_specific["agent_runtime_turn_token"] = (
            "actual-restored-runtime"
        )
        task = restarted.on_native_terminal(
            terminal_context,
            outcome="terminal",
        )
        if task is not None:
            await task

    asyncio.run(terminalize_restored_native())
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, str(outcome.turn_id))
    assert terminal is not None
    assert terminal["state"] == "terminal"
    assert dispatch_calls == 1


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
    manager._active_identity = lambda *_args: None
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


def test_explicit_empty_p0_creates_control_delivery_without_message(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="",
            ),
            context=_context(),
        )
    )

    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(result.delivery_id))
        message_count = conn.execute(select(messages.c.id)).all()
    assert result.message_id is None
    assert delivery is not None
    assert delivery["message_id"] is None
    assert message_count == []


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
    assert rebound["runtime_turn_id"] == "runtime-token"
    restarted._steer.assert_awaited_once()

    async def terminalize_restored_native() -> None:
        terminal_context = _context()
        terminal_context.platform_specific["turn_token"] = turn_id
        terminal_context.platform_specific["agent_runtime_turn_token"] = "runtime-token"
        task = restarted.on_native_terminal(
            terminal_context,
            outcome="terminal",
        )
        if task is not None:
            await task

    asyncio.run(terminalize_restored_native())
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, turn_id)
    assert terminal is not None
    assert terminal["state"] == "terminal"


def test_p1_during_unbound_start_steers_once_after_native_binding(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="starting"),
            context=_context(),
        )
    )
    assert admitted.turn_id
    steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    manager._steer = steer

    follow_up = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="do not duplicate"),
            context=_context(),
        )
    )

    row = next(item for item in _delivery_rows(engine) if item["id"] == follow_up.delivery_id)
    assert follow_up.state == "pending_steer"
    assert row["priority"] == "p1"
    assert row["target_turn_id"] == admitted.turn_id
    assert row["receipt_outcome"] is None
    steer.assert_not_awaited()

    async def bind_native() -> None:
        context = _context()
        context.platform_specific["turn_token"] = str(admitted.turn_id)
        manager._active_identity = lambda _backend, _session, logical: (
            logical,
            "native-started",
        )
        task = manager.on_native_start(
            context,
            backend="codex",
            runtime_key="runtime-key",
            runtime_turn_id="runtime-turn",
        )
        assert task is not None
        await task

    asyncio.run(bind_native())
    row = next(item for item in _delivery_rows(engine) if item["id"] == follow_up.delivery_id)
    assert row["state"] == "attached"
    assert row["receipt_outcome"] == "accepted"
    steer.assert_awaited_once()


def test_p1_pending_native_binding_falls_back_once_on_terminal_proof(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="starting"),
            context=_context(),
        )
    )
    steer = AsyncMock()
    manager._steer = steer
    follow_up = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="fallback once"),
            context=_context(),
        )
    )

    async def prove_terminal() -> None:
        context = _context()
        context.platform_specific["turn_token"] = str(admitted.turn_id)
        task = manager.on_native_terminal(context, outcome="terminal")
        assert task is not None
        await task

    asyncio.run(prove_terminal())
    row = next(item for item in _delivery_rows(engine) if item["id"] == follow_up.delivery_id)
    assert row["priority"] == "p3"
    assert row["state"] == "starting"
    assert row["receipt_outcome"] == "not_active"
    assert row["target_turn_id"] != admitted.turn_id
    assert len(starts) == 2
    steer.assert_not_awaited()


def test_restart_steers_prewrite_p1_once_after_exact_native_evidence(managers) -> None:
    manager, restored, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="starting"),
            context=_context(),
        )
    )
    follow_up = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="after restart"),
            context=_context(),
        )
    )
    restored._active_identity = lambda _backend, _session, logical: (
        logical,
        "native-restored",
    )
    restored._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    asyncio.run(restored.recover_durable_delivery_state("ses_fsm"))

    row = next(item for item in _delivery_rows(engine) if item["id"] == follow_up.delivery_id)
    assert row["state"] == "attached"
    assert row["target_turn_id"] == admitted.turn_id
    assert row["expected_native_turn_id"] == "native-restored"
    restored._steer.assert_awaited_once()


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


def test_p0_successor_prewrite_failure_releases_stale_successor_reference(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def prepare_successor():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        delivery = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replacement",
            ),
            context=_context(),
        )
        claimed = manager._terminalize_durable_turn(turn_id, "canceled")
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return delivery, str(claimed["successor_turn_id"])

    delivery, successor_id = asyncio.run(prepare_successor())
    assert successor_id

    def missing_context(_session_id):
        raise LookupError("session routing unavailable")

    manager._build_context = missing_context
    assert not asyncio.run(manager._start_persisted_turn(successor_id))

    with engine.connect() as conn:
        queued = delivery_store.get_delivery(conn, str(delivery.delivery_id))
    assert queued is not None
    assert queued["state"] == "queued"
    assert queued["target_turn_id"] is None
    assert queued["successor_turn_id"] is None


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


def test_empty_p0_natural_terminal_race_preserves_fifo_queue(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    release_terminal = asyncio.Event()
    interrupt_persisted = asyncio.Event()
    release_interrupt = asyncio.Event()

    async def terminal_after_p0(*_args, **_kwargs):
        await release_terminal.wait()
        return SimpleNamespace(settled_by="terminal_result")

    async def delayed_interrupt(_session_id, _turn_id, delivery_id):
        interrupt_persisted.set()
        await release_interrupt.wait()
        with engine.connect() as conn:
            delivery = delivery_store.get_delivery(conn, delivery_id)
        return {"state": str((delivery or {}).get("state") or "reconciling")}

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        terminal_after_p0,
    )
    manager._interrupt_durable_turn = delayed_interrupt

    async def run():
        active_context = _context()
        active = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="active",
            ),
            context=active_context,
        )
        manager._active_identity = lambda _backend, _session, logical: (
            logical,
            "native-active",
        )
        assert (
            manager.on_native_start(
                active_context,
                backend="codex",
                runtime_key="runtime-active",
                runtime_turn_id="runtime-turn-active",
            )
            is None
        )
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="must remain queued",
            ),
            context=_context(),
        )
        p0 = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p0"),
                context=_context(),
            )
        )
        await interrupt_persisted.wait()
        holder = manager.in_flight["ses_fsm"].task
        release_terminal.set()
        await holder
        release_interrupt.set()
        await p0
        await asyncio.sleep(0)
        return active, queued

    _active, queued = asyncio.run(run())
    with engine.connect() as conn:
        queued_row = delivery_store.get_delivery(conn, str(queued.delivery_id))
        owner = delivery_store.active_turn(conn, "ses_fsm")
    assert queued_row is not None
    assert queued_row["state"] == "queued"
    assert owner is None


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


def test_session_delete_cascades_owners_and_preserves_message(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="preserve immutable content",
            ),
            context=_context(),
        )
    )

    with engine.begin() as conn:
        conn.execute(
            agent_sessions.delete().where(agent_sessions.c.id == "ses_fsm")
        )

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
        delivery = delivery_store.get_delivery(conn, str(admitted.delivery_id))
        message = conn.execute(
            select(messages).where(messages.c.id == admitted.message_id)
        ).mappings().one()
    assert turn is None
    assert delivery is None
    assert message["session_id"] is None
    assert message["content_text"] == "preserve immutable content"


def test_existing_message_replays_persisted_dispatch_text_after_p1_fallback(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    dispatched: list[str] = []

    async def capture_run(_session_id, _context_value, text, **_kwargs):
        dispatched.append(text)

    manager._run = capture_run
    with engine.begin() as conn:
        message = messages_service.append(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            platform="avibe",
            author="user",
            source="user",
            message_type=messages_service.PENDING_TYPE,
            text="transcript projection",
        )

    async def run() -> DeliveryResult:
        turn_id, _context_value = await _activate(manager)
        dispatched.clear()
        manager._steer = AsyncMock(
            return_value=steer_result(SteerOutcome.REFUSED, reason="not_steerable")
        )
        delivery = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                message_id=str(message["id"]),
                content="attachment-enriched dispatch text",
            ),
            context=_context(),
        )
        assert delivery.state == "queued"
        assert manager._terminalize_durable_turn(turn_id, "completed")["changed"]
        assert await manager.drain_delivery_queue("ses_fsm")
        return delivery

    delivery = asyncio.run(run())
    with engine.connect() as conn:
        owner = delivery_store.get_delivery(conn, str(delivery.delivery_id))
        immutable = messages_service.get_message(
            conn,
            str(message["id"]),
            session_id="ses_fsm",
        )
    assert dispatched == ["attachment-enriched dispatch text"]
    assert owner is not None
    assert owner["dispatch_text"] == "attachment-enriched dispatch text"
    assert immutable is not None
    assert immutable["text"] == "transcript projection"


def test_terminal_completion_preserves_accepted_steer_receipt(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        turn_id, _context_value = await _activate(manager)
        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
        delivery = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="attach once",
            ),
            context=_context(),
        )
        assert delivery.state == "attached"
        assert manager._terminalize_durable_turn(turn_id, "completed")["changed"]
        return delivery, turn_id

    delivery, turn_id = asyncio.run(run())
    with engine.connect() as conn:
        owner = delivery_store.get_delivery(conn, str(delivery.delivery_id))
        turn = delivery_store.get_turn(conn, turn_id)
    assert owner is not None
    assert owner["state"] == "completed"
    assert owner["receipt_outcome"] == "accepted"
    assert turn is not None
    assert turn["terminal_outcome"] == "completed"


def test_status_projection_leaves_ownerless_restored_runtime_running(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            agent_sessions.update()
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )

    manager.project_durable_agent_status()
    with engine.connect() as conn:
        ownerless = conn.execute(
            select(agent_sessions.c.agent_status).where(agent_sessions.c.id == "ses_fsm")
        ).scalar_one()
    assert ownerless == "running"

    with engine.begin() as conn:
        delivery_store.insert_turn(
            conn,
            turn_id="trn_terminal_history",
            session_id="ses_fsm",
            state="terminal",
            backend="opencode",
        )
    manager.project_durable_agent_status()
    with engine.connect() as conn:
        projected = conn.execute(
            select(agent_sessions.c.agent_status).where(agent_sessions.c.id == "ses_fsm")
        ).scalar_one()
    assert projected == "idle"


def test_restored_terminal_result_resumes_durable_fifo(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="resume after restored terminal",
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        manager.is_active_emit = lambda _context_value: True
        manager.on_terminal_result(context, is_error=False)
        for _ in range(4):
            await asyncio.sleep(0)
        return queued, turn_id

    queued, turn_id = asyncio.run(run())
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, turn_id)
        resumed = delivery_store.get_delivery(conn, str(queued.delivery_id))
    assert terminal is not None
    assert terminal["state"] == "terminal"
    assert resumed is not None
    assert resumed["state"] == "starting"
    assert len(starts) == 2


def test_terminal_resume_selects_oldest_combined_fifo_head(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    dispatched: list[str] = []

    async def run():
        turn_id, context = await _activate(manager)
        durable = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="older durable row",
            ),
            context=_context(),
        )
        assert durable.state == "queued"
        with engine.begin() as conn:
            legacy = messages_service.enqueue_queued(
                conn,
                scope_id=None,
                session_id="ses_fsm",
                text="newer legacy row",
            )
            conn.execute(
                messages.update()
                .where(messages.c.id == legacy["id"])
                .values(created_at="9999-12-31T23:59:59Z")
            )

        async def capture_run(_session_id, _context_value, text, **_kwargs):
            dispatched.append(text)

        manager._run = capture_run
        terminal_resume = manager.on_native_terminal(context, outcome="completed")
        assert terminal_resume is not None
        await terminal_resume
        return durable, turn_id

    durable, turn_id = asyncio.run(run())
    assert dispatched == ["older durable row"]
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, turn_id)
        resumed = delivery_store.get_delivery(conn, str(durable.delivery_id))
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert terminal is not None
    assert terminal["state"] == "terminal"
    assert resumed is not None
    assert resumed["state"] == "starting"
    assert [row["text"] for row in queued] == ["newer legacy row"]


def test_busy_sessions_include_restored_durable_owner_without_runtime_task(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))

    assert manager.in_flight == {}
    assert manager.busy_session_ids() == {"ses_fsm"}

    with manager._sqlite_engine().connect() as conn:
        owner = delivery_store.active_turn(conn, "ses_fsm")
    assert owner is not None
    assert owner["id"] == turn_id


def test_p0_latches_until_starting_turn_binds_native_identity(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def hold_start(_turn_id, *, context=None):
        return False

    manager._start_persisted_turn = hold_start

    async def run():
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="starting"),
            context=_context(),
        )
        assert admitted.turn_id
        p0 = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
            context=_context(),
        )
        assert p0.state == "interrupt_pending"
        manager.controller.command_handler.handle_stop.assert_not_awaited()

        context = _context()
        context.platform_specific["turn_token"] = str(admitted.turn_id)
        context.platform_specific["agent_runtime_turn_token"] = "runtime-token"
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=str(admitted.turn_id),
        )
        manager._active_identity = lambda _backend, _session, logical: (
            logical,
            "native-started",
        )
        interrupt = manager.on_native_start(
            context,
            backend="codex",
            runtime_key="runtime-key",
            runtime_turn_id="runtime-token",
        )
        assert interrupt is not None
        await interrupt
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return p0

    p0 = asyncio.run(run())
    with engine.connect() as conn:
        owner = delivery_store.get_delivery(conn, str(p0.delivery_id))
    assert owner is not None
    assert owner["state"] == "waiting_terminal"
    assert owner["receipt_outcome"] == "accepted"
    manager.controller.command_handler.handle_stop.assert_awaited_once()


def test_pending_p0_claims_successor_when_old_start_proves_prewrite_failure(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    start = manager._start_persisted_turn

    async def hold_start(_turn_id, *, context=None):
        return False

    manager._start_persisted_turn = hold_start

    async def admit():
        old = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="old"),
            context=_context(),
        )
        replacement = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
            context=_context(),
        )
        return old, replacement

    old, replacement = asyncio.run(admit())
    assert old.turn_id
    assert replacement.state == "interrupt_pending"
    manager._start_persisted_turn = start
    build_context = manager._build_context
    calls = 0

    def fail_old_start_once(session_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LookupError("old Turn routing unavailable")
        return build_context(session_id)

    dispatched: list[str] = []

    async def capture_run(_session_id, _context_value, text, **_kwargs):
        dispatched.append(text)

    manager._build_context = fail_old_start_once
    manager._run = capture_run
    assert not asyncio.run(manager._start_persisted_turn(str(old.turn_id)))

    with engine.connect() as conn:
        old_turn = delivery_store.get_turn(conn, str(old.turn_id))
        owner = delivery_store.get_delivery(conn, str(replacement.delivery_id))
        successor = delivery_store.get_turn(
            conn,
            str((owner or {}).get("successor_turn_id") or ""),
        )
    assert old_turn is not None
    assert old_turn["state"] == "terminal"
    assert old_turn["terminal_outcome"] == "pre_write_failure"
    assert owner is not None
    assert owner["state"] == "starting"
    assert successor is not None
    assert successor["state"] == "starting"
    assert dispatched == ["replacement"]
    manager.controller.command_handler.handle_stop.assert_not_awaited()


def test_restart_consumes_pending_p0_after_native_evidence_rebind(managers) -> None:
    manager, restored, engine, _engine_b, _starts = managers

    async def hold_start(_turn_id, *, context=None):
        return False

    manager._start_persisted_turn = hold_start

    async def admit():
        old = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="old"),
            context=_context(),
        )
        pending = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
            context=_context(),
        )
        return old, pending

    old, pending = asyncio.run(admit())
    assert old.turn_id
    assert pending.state == "interrupt_pending"

    async def recover():
        context = _context()
        context.platform_specific["turn_token"] = str(old.turn_id)
        holder = asyncio.create_task(asyncio.Event().wait())
        restored.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=str(old.turn_id),
        )
        restored._active_identity = lambda _backend, _session, logical: (
            logical,
            "native-restored",
        )
        await restored.recover_durable_delivery_state("ses_fsm")
        await restored.recover_durable_delivery_state("ses_fsm")
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)

    asyncio.run(recover())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(old.turn_id))
        owner = delivery_store.get_delivery(conn, str(pending.delivery_id))
    assert turn is not None
    assert turn["state"] == "active"
    assert turn["native_turn_id"] == "native-restored"
    assert owner is not None
    assert owner["state"] == "waiting_terminal"
    assert owner["receipt_outcome"] == "accepted"
    restored.controller.command_handler.handle_stop.assert_awaited_once()


def test_p0_interrupts_exact_restored_owner_without_in_flight_task(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))

    replacement = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replace restored owner",
            ),
            context=_context(),
        )
    )

    assert replacement.state == "waiting_terminal"
    manager.controller.command_handler.handle_stop.assert_awaited_once()
    stop_context = manager.controller.command_handler.handle_stop.await_args.args[0]
    assert stop_context.platform_specific["turn_token"] == turn_id
    assert stop_context.platform_specific["agent_runtime_turn_key"] == "runtime-key"
    assert stop_context.platform_specific["agent_runtime_turn_token"] == "runtime-token"
    with engine.connect() as conn:
        owner = delivery_store.get_delivery(conn, str(replacement.delivery_id))
    assert owner is not None
    assert owner["state"] == "waiting_terminal"
    assert owner["target_turn_id"] == turn_id


def test_empty_p1_promotes_fifo_with_persisted_dispatch_text(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        message = messages_service.append(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            platform="avibe",
            author="user",
            source="user",
            message_type=messages_service.PENDING_TYPE,
            text="transcript projection",
        )

    async def run():
        await _activate(manager)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                message_id=str(message["id"]),
                content="attachment-enriched FIFO text",
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
        promoted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1"),
            context=_context(),
        )
        return promoted

    promoted = asyncio.run(run())
    assert promoted.state == "attached"
    request = manager._steer.await_args.args[1]
    assert request.text == "attachment-enriched FIFO text"


def test_empty_p1_promotes_oldest_legacy_row_before_durable_fifo(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        await _activate(manager)
        with engine.begin() as conn:
            legacy = messages_service.enqueue_queued(
                conn,
                scope_id=None,
                session_id="ses_fsm",
                text="older legacy row",
            )
        durable = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="newer durable row",
            ),
            context=_context(),
        )
        manager._steer = AsyncMock(
            return_value=steer_result(SteerOutcome.ACCEPTED)
        )
        promoted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1"),
            context=_context(),
        )
        return legacy, durable, promoted

    legacy, durable, promoted = asyncio.run(run())
    assert promoted.state == "attached"
    assert promoted.message_id == legacy["id"]
    assert manager._steer.await_args.args[1].text == "older legacy row"
    with engine.connect() as conn:
        newer = delivery_store.get_delivery(conn, str(durable.delivery_id))
    assert newer is not None
    assert newer["state"] == "queued"


def test_archive_retires_durable_owners_before_clearing_queue(managers) -> None:
    from storage.workbench_sessions_service import archive_session

    manager, _other, engine, _engine_b, _starts = managers

    async def admit():
        await _activate(manager)
        return await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="unsent"),
            context=_context(),
        )

    queued = asyncio.run(admit())
    assert queued.state == "queued"
    with engine.begin() as conn:
        archive_session(conn, "ses_fsm")
    with engine.connect() as conn:
        queued_message = messages_service.get_message(
            conn,
            str(queued.message_id),
            session_id="ses_fsm",
        )
        deliveries = list(
            conn.execute(
                select(session_deliveries).where(session_deliveries.c.session_id == "ses_fsm")
            ).mappings()
        )
        turns = list(
            conn.execute(
                select(session_turns).where(session_turns.c.session_id == "ses_fsm")
            ).mappings()
        )
    assert queued_message is None
    assert deliveries
    assert all(row["state"] == "completed" for row in deliveries)
    assert all(row["message_id"] is None for row in deliveries)
    assert turns
    assert all(row["state"] == "terminal" for row in turns)


def test_claimed_start_rechecks_archive_before_backend_dispatch(managers) -> None:
    from storage.workbench_sessions_service import archive_session

    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        turn_id, _active_context = await _activate(manager)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="must not dispatch after archive",
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        assert manager._terminalize_durable_turn(turn_id, "completed")["changed"]
        with engine.begin() as conn:
            conn.execute(
                session_deliveries.update()
                .where(session_deliveries.c.id == queued.delivery_id)
                .values(target_turn_id=turn_id)
            )

        def archive_while_building_context(_session_id):
            with engine.begin() as conn:
                archive_session(conn, "ses_fsm")
            return _context()

        manager._delivery_context = archive_while_building_context
        manager._run = AsyncMock()
        started = await manager.drain_delivery_queue("ses_fsm")
        return queued, started

    queued, started = asyncio.run(run())
    assert started is False
    manager._run.assert_not_awaited()
    with engine.connect() as conn:
        session_status = conn.execute(
            select(agent_sessions.c.status).where(agent_sessions.c.id == "ses_fsm")
        ).scalar_one()
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
    assert session_status == "archived"
    assert delivery is not None
    assert delivery["state"] == "completed"


def test_native_start_binding_failure_stays_in_reconciliation_boundary(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def hold_start(_turn_id, *, context=None):
        return False

    manager._start_persisted_turn = hold_start
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="native accepted"),
            context=_context(),
        )
    )
    assert admitted.turn_id
    context = _context()
    context.platform_specific["turn_token"] = str(admitted.turn_id)
    manager._active_identity = lambda _backend, _session, logical: (
        logical,
        "native-written",
    )

    def unavailable_bind(*_args, **_kwargs):
        raise RuntimeError("ownership database unavailable")

    monkeypatch.setattr(delivery_store, "bind_native_start", unavailable_bind)
    assert (
        manager.on_native_start(
            context,
            backend="codex",
            runtime_key="runtime-key",
            runtime_turn_id="runtime-turn",
        )
        is None
    )
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert turn is not None
    assert turn["state"] == "starting"
    assert turn["native_turn_id"] is None


def test_restart_retires_ownerless_legacy_turn_and_ignores_late_terminal(
    managers,
    monkeypatch,
) -> None:
    manager, restored, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    release = asyncio.Event()

    async def blocked_legacy_dispatch(*_args, **_kwargs):
        await release.wait()
        return SimpleNamespace(settled_by="terminal_result")

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        blocked_legacy_dispatch,
    )

    async def run():
        await manager._run("ses_fsm", _context(), "legacy public turn")
        await asyncio.sleep(0)
        with engine.connect() as conn:
            owner = delivery_store.active_turn(conn, "ses_fsm")
            assert owner is not None
            assert not delivery_store.turn_has_delivery_owner(conn, str(owner["id"]))
        restored._active_identity = lambda *_args: None
        await restored.recover_durable_delivery_state("ses_fsm")
        release.set()
        await manager.in_flight["ses_fsm"].task
        return str(owner["id"])

    turn_id = asyncio.run(run())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        active = delivery_store.active_turn(conn, "ses_fsm")
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "ownerless_legacy_restart"
    assert active is None


def test_ownerless_recovery_resumes_oldest_combined_fifo_head(managers) -> None:
    manager, restored, engine, _engine_b, _starts = managers
    dispatched: list[str] = []
    with engine.begin() as conn:
        delivery_store.insert_turn(
            conn,
            turn_id="trn_ownerless",
            session_id="ses_fsm",
            state="active",
            backend="codex",
        )
        legacy = messages_service.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="older legacy row",
        )
        conn.execute(
            messages.update()
            .where(messages.c.id == legacy["id"])
            .values(created_at="0001-01-01T00:00:00Z")
        )

    async def run():
        durable = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="newer durable row",
            ),
            context=_context(),
        )
        assert durable.state == "queued"

        async def capture_run(_session_id, _context_value, text, **_kwargs):
            dispatched.append(text)

        restored._run = capture_run
        restored._active_identity = lambda *_args: None
        await restored.recover_durable_delivery_state("ses_fsm")
        return durable

    durable = asyncio.run(run())
    assert dispatched == ["older legacy row"]
    with engine.connect() as conn:
        ownerless = delivery_store.get_turn(conn, "trn_ownerless")
        queued = delivery_store.get_delivery(conn, str(durable.delivery_id))
    assert ownerless is not None
    assert ownerless["state"] == "terminal"
    assert ownerless["terminal_outcome"] == "ownerless_legacy_restart"
    assert queued is not None
    assert queued["state"] == "queued"


def test_restart_resumes_queued_delivery_after_terminal_owner_was_committed(
    managers,
) -> None:
    manager, restored, engine, _engine_b, _starts = managers
    dispatched: list[str] = []

    async def run():
        turn_id, _context_value = await _activate(manager)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="resume after terminal commit",
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        assert manager._terminalize_durable_turn(turn_id, "completed")["changed"]

        async def capture_run(_session_id, _context_value, text, **_kwargs):
            dispatched.append(text)

        restored._run = capture_run
        await restored.recover_durable_delivery_state("ses_fsm")
        return queued

    queued = asyncio.run(run())
    assert dispatched == ["resume after terminal commit"]
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
        active = delivery_store.active_turn(conn, "ses_fsm")
    assert delivery is not None
    assert delivery["state"] == "starting"
    assert active is not None
    assert active["id"] == delivery["target_turn_id"]


def test_terminal_reconciliation_waits_for_release_and_contains_write_failure(
    managers,
    monkeypatch,
) -> None:
    manager, _other, _engine, _engine_b, _starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        done = asyncio.Event()
        sink = {"done_event": done, "turn_token": turn_id}
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        manager.controller.get_turn_sink = lambda _session_key: sink
        calls = 0

        def unavailable_terminal_write(_turn_id, _outcome):
            nonlocal calls
            calls += 1
            raise RuntimeError("ownership database unavailable")

        monkeypatch.setattr(manager, "_terminalize_durable_turn", unavailable_terminal_write)
        manager.on_terminal_result(context, is_error=False)
        assert calls == 0
        sink["settled_by"] = "terminal_result"
        done.set()
        for _ in range(3):
            await asyncio.sleep(0)
        assert calls == 1
        assert manager.controller.statuses[-1] == ("ses_fsm", "idle")

    asyncio.run(run())


def test_runtime_release_cannot_override_sink_terminal_outcome(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        done = asyncio.Event()
        sink = {"done_event": done, "turn_token": turn_id}
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        manager.controller.get_turn_sink = lambda _session_key: sink
        manager.on_terminal_result(context, is_error=False)
        sink["settled_by"] = "terminal_result"
        done.set()

        manager.on_native_terminal(context, outcome="terminal")
        for _ in range(3):
            await asyncio.sleep(0)
        return turn_id

    turn_id = asyncio.run(run())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "completed"


def test_runner_preserves_sink_error_outcome_when_it_terminalizes_first(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)

    async def error_result(_controller, context, _text, **_kwargs):
        done = asyncio.Event()
        sink = {
            "done_event": done,
            "turn_token": context.platform_specific["turn_token"],
        }
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        manager.controller.get_turn_sink = lambda _session_key: sink
        manager.on_terminal_result(context, is_error=True)
        sink["settled_by"] = "terminal_result"
        done.set()
        return SimpleNamespace(settled_by="terminal_result")

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        error_result,
    )

    async def run() -> str:
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="fails"),
            context=_context(),
        )
        await manager.in_flight["ses_fsm"].task
        for _ in range(3):
            await asyncio.sleep(0)
        return str(admitted.turn_id)

    turn_id = asyncio.run(run())
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "failed"


def test_stop_terminal_result_preserves_canceled_turn_and_accepted_receipt(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        done = asyncio.Event()
        sink = {"done_event": done, "turn_token": turn_id}
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        manager.controller.get_turn_sink = lambda _session_key: sink
        manager.is_active_emit = lambda _context_value: True
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )

        async def stop_with_terminal(stop_context):
            manager.on_terminal_result(stop_context, is_error=False)
            sink["settled_by"] = "stopped"
            done.set()
            await asyncio.sleep(0)
            return True

        manager.controller.command_handler.handle_stop = AsyncMock(
            side_effect=stop_with_terminal
        )
        replacement = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replacement",
            ),
            context=_context(),
        )
        for _ in range(3):
            await asyncio.sleep(0)
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return turn_id, replacement

    turn_id, replacement = asyncio.run(run())
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, turn_id)
        delivery = delivery_store.get_delivery(conn, str(replacement.delivery_id))
    assert terminal is not None
    assert terminal["terminal_outcome"] == "canceled"
    assert delivery is not None
    assert delivery["state"] == "starting"
    assert delivery["receipt_outcome"] == "accepted"


def test_stopping_agent_initiated_turn_preserves_durable_queue(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        context = _context()
        context.platform_specific.update(
            {
                "turn_token": "trn-agent-initiated",
                "agent_runtime_turn_key": "agent-initiated-key",
                "agent_runtime_turn_token": "agent-initiated-token",
            }
        )
        assert manager.register_agent_initiated_turn(context)
        await asyncio.sleep(0)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="stay queued after Stop",
            ),
            context=_context(),
        )
        holder = manager.in_flight["ses_fsm"].task
        stopped = await manager.cancel("ses_fsm")
        await asyncio.gather(holder, return_exceptions=True)
        return queued, stopped

    queued, stopped = asyncio.run(run())
    assert stopped["status"] == "cancel_requested"
    assert starts == []
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
        active = delivery_store.active_turn(conn, "ses_fsm")
    assert delivery is not None
    assert delivery["state"] == "queued"
    assert active is None


def test_agent_initiated_completion_resumes_oldest_combined_fifo_head(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    dispatched: list[str] = []

    async def run():
        manager.controller._get_session_key = lambda _context_value: "avibe::ses_fsm"
        context = _context()
        context.platform_specific.update(
            {
                "turn_token": "trn-agent-initiated-fifo",
                "agent_runtime_turn_key": "agent-initiated-key",
                "agent_runtime_turn_token": "agent-initiated-token",
            }
        )
        assert manager.register_agent_initiated_turn(context)
        await asyncio.sleep(0)
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="older durable row",
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        with engine.begin() as conn:
            legacy = messages_service.enqueue_queued(
                conn,
                scope_id=None,
                session_id="ses_fsm",
                text="newer legacy row",
            )
            conn.execute(
                messages.update()
                .where(messages.c.id == legacy["id"])
                .values(created_at="9999-12-31T23:59:59Z")
            )

        async def capture_run(_session_id, _context_value, text, **_kwargs):
            dispatched.append(text)

        manager._run = capture_run
        terminalize = manager._terminalize_durable_turn

        def commit_then_fail(turn_id, outcome):
            terminalize(turn_id, outcome)
            raise RuntimeError("terminal result owner became temporarily unavailable")

        manager._terminalize_durable_turn = commit_then_fail
        holder = manager.in_flight["ses_fsm"].task
        sink = manager.get_turn_sink("avibe::ses_fsm")
        assert sink is not None
        sink["settled_by"] = "terminal_result"
        sink["done_event"].set()
        await holder
        return queued, legacy

    queued, legacy = asyncio.run(run())
    assert dispatched == ["older durable row"]
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
        legacy_row = messages_service.get_message(conn, str(legacy["id"]))
    assert delivery is not None
    assert delivery["state"] == "starting"
    assert legacy_row is not None
    assert legacy_row["type"] == messages_service.QUEUED_TYPE


def test_backend_refresh_releases_restored_durable_runtime_owner(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        delivery_store.insert_turn(
            conn,
            turn_id="trn-restored-runtime",
            session_id="ses_fsm",
            state="starting",
            backend="codex",
        )
        delivery_store.insert_delivery(
            conn,
            delivery_id="dlv-restored-runtime",
            session_id="ses_fsm",
            message_id=None,
            dispatch_text="restored",
            priority="p3",
            state="starting",
            target_turn_id="trn-restored-runtime",
        )
        bound = delivery_store.bind_native_start(
            conn,
            "trn-restored-runtime",
            expected_version=1,
            runtime_key="runtime-key",
            runtime_turn_id="runtime-turn",
            native_turn_id="native-turn",
        )
        assert bound is not None

    assert manager.active_runtime_session_ids_for_backend("codex") == {"ses_fsm"}
    manager.begin_backend_drain("codex")
    released = asyncio.run(
        manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={"ses_fsm"},
        )
    )
    assert released == 1
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, "trn-restored-runtime")
        delivery = delivery_store.get_delivery(conn, "dlv-restored-runtime")
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "backend_refresh"
    assert delivery is not None
    assert delivery["state"] == "completed"
    assert manager._deferred_restart_sessions["codex"] == {"ses_fsm"}


def test_backend_drain_defers_durable_successor_start_attempt(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        manager.begin_backend_drain("codex")
        replacement = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="after refresh",
            ),
            context=_context(),
        )
        assert replacement.state == "waiting_terminal"
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        manager.in_flight.pop("ses_fsm", None)
        terminal_task = manager.on_native_terminal(context, outcome="canceled")
        assert terminal_task is not None
        await terminal_task

        with engine.connect() as conn:
            delivery = delivery_store.get_delivery(conn, str(replacement.delivery_id))
            successor = delivery_store.get_turn(
                conn,
                str((delivery or {}).get("successor_turn_id") or ""),
            )
        assert successor is not None
        assert successor["state"] == "starting"
        assert successor["start_attempt_id"] is None
        assert starts == [turn_id]

        await manager.end_backend_drain("codex")
        return str(successor["id"])

    successor_id = asyncio.run(run())
    with engine.connect() as conn:
        successor = delivery_store.get_turn(conn, successor_id)
    assert successor is not None
    assert successor["start_attempt_id"] is not None
    assert len(starts) == 2
    assert starts[1] == successor_id


def test_legacy_submit_queues_behind_restored_durable_owner(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, _context_value = await _activate(manager)

        def enqueue() -> bool:
            with engine.begin() as conn:
                messages_service.enqueue_queued(
                    conn,
                    scope_id=None,
                    session_id="ses_fsm",
                    text="wait for restored native work",
                )
            return True

        result = await manager.submit(
            "ses_fsm",
            _context(),
            "wait for restored native work",
            enqueue=enqueue,
        )
        return turn_id, result

    turn_id, result = asyncio.run(run())
    assert result.route == "enqueued"
    assert result.queue_persisted is True
    assert result.target_was_busy is True
    assert starts == [turn_id]
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert [row["text"] for row in queued] == ["wait for restored native work"]


def test_send_now_defers_behind_restored_durable_owner(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers

    async def run():
        turn_id, _context_value = await _activate(manager)

        def enqueue() -> bool:
            with engine.begin() as conn:
                messages_service.enqueue_queued(
                    conn,
                    scope_id=None,
                    session_id="ses_fsm",
                    text="send after restored native work",
                )
            return True

        manager.send_now = AsyncMock()
        result = await manager.submit(
            "ses_fsm",
            _context(),
            "send after restored native work",
            enqueue=enqueue,
            delivery_intent="send_now",
        )
        return turn_id, result

    turn_id, result = asyncio.run(run())
    assert result.route == "enqueued"
    assert result.queue_persisted is True
    assert result.target_was_busy is True
    assert result.delivery_status == "deferred"
    manager.send_now.assert_not_awaited()
    assert starts == [turn_id]
    with engine.connect() as conn:
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert [row["text"] for row in queued] == ["send after restored native work"]


def test_idle_p3_preserves_older_legacy_fifo_head(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        messages_service.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="older legacy row",
        )
    dispatched: list[str] = []

    async def capture_run(_session_id, _context_value, text, **_kwargs):
        dispatched.append(text)

    manager._run = capture_run
    delivery = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="new durable row",
            ),
            context=_context(),
        )
    )
    assert delivery.state == "queued"
    assert dispatched == ["older legacy row"]
    with engine.connect() as conn:
        owner = delivery_store.get_delivery(conn, str(delivery.delivery_id))
        queued = messages_service.list_queued(conn, "ses_fsm")
    assert owner is not None
    assert owner["state"] == "queued"
    assert [row["id"] for row in queued] == [delivery.message_id]


def test_remove_queued_retires_durable_delivery_before_message(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run():
        await _activate(manager)
        return await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="remove me",
            ),
            context=_context(),
        )

    queued = asyncio.run(run())
    assert queued.state == "queued"
    assert queued.message_id
    with engine.begin() as conn:
        assert messages_service.remove_queued(
            conn,
            "ses_fsm",
            str(queued.message_id),
        )
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(queued.delivery_id))
        message = messages_service.get_message(
            conn,
            str(queued.message_id),
            session_id="ses_fsm",
        )
    assert delivery is not None
    assert delivery["state"] == "completed"
    assert delivery["message_id"] is None
    assert delivery["receipt_outcome"] == "removed"
    assert message is None
