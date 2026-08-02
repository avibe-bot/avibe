from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from core.services.agent_steering import SteerOutcome, result as steer_result
from core.services.dispatch import TurnDispatchOutcome
from core.native_dispatch_phase import DISPATCH_PHASE_PREWRITE, set_dispatch_phase
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from core.session_turns import (
    SCHEDULED_PROVENANCE_KEY,
    SOURCE_SCHEDULED,
    DeliveryRequest,
    SessionTurnManager,
    Turn,
)
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
    priority: str = "p3",
) -> tuple[str, MessageContext]:
    context = _context()
    admitted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority=priority, content=text),
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


def test_fifo_segment_starts_one_turn_and_materializes_one_merged_message(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    active_turn_id, _ = asyncio.run(_activate(manager, text="active"))
    queued = [
        asyncio.run(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content=text,
                    metadata={"_web_push_user_key": user_key},
                ),
                context=_context(),
            )
        )
        for text, user_key in (
            ("queued A", "remote:user-a"),
            ("queued B", "remote:user-b"),
        )
    ]

    assert asyncio.run(manager.terminalize_turn(active_turn_id))
    queued_starts = [(turn_id, text) for turn_id, text in starts if turn_id != active_turn_id]
    assert len(queued_starts) == 1
    turn_id, dispatch_text = queued_starts[0]
    assert dispatch_text == "queued A\nqueued B"

    claimed = [_row(engine, str(item.delivery_id)) for item in queued]
    assert [row["turn_id"] for row in claimed] == [turn_id, turn_id]
    assert [row["turn_role"] for row in claimed] == ["initial", "initial"]
    assert [row["turn_position"] for row in claimed] == [0, 1]
    assert [row["state"] for row in claimed] == ["claimed", "claimed"]
    assert all(row["current_attempt_id"] is None for row in claimed)

    context = _context()
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

    accepted = [_row(engine, str(item.delivery_id)) for item in queued]
    assert accepted[0]["message_id"] == accepted[1]["message_id"]
    assert accepted[0]["message_id"] is not None
    with engine.connect() as conn:
        stored = conn.execute(
            select(messages).where(messages.c.id == accepted[0]["message_id"])
        ).mappings().one()
        merged_rows = conn.execute(
            select(messages.c.id).where(
                messages.c.session_id == "ses_fsm",
                messages.c.content_text.in_(("queued A", "queued B", "queued A\nqueued B")),
            )
        ).all()
        assert merged_rows == [(accepted[0]["message_id"],)]
    assert stored["content_text"] == "queued A\nqueued B"
    assert json.loads(stored["metadata_json"])["_web_push_user_keys"] == [
        "remote:user-a",
        "remote:user-b",
    ]
    assert stored["created_at"] == accepted[0]["submitted_at"]
    assert stored["delivered_at"] == accepted[0]["materialized_at"]


def test_fifo_segment_does_not_merge_different_message_authors(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    active_turn_id, _ = asyncio.run(_activate(manager, text="active"))
    queued = [
        asyncio.run(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content=text,
                    author_id=author_id,
                    author_name=author_name,
                ),
                context=_context(),
            )
        )
        for text, author_id, author_name in (
            ("from Alice", "remote:alice", "Alice"),
            ("from Bob", "remote:bob", "Bob"),
        )
    ]

    assert asyncio.run(manager.terminalize_turn(active_turn_id))
    queued_starts = [(turn_id, text) for turn_id, text in starts if turn_id != active_turn_id]
    assert len(queued_starts) == 1
    alice_turn_id, dispatch_text = queued_starts[0]
    assert dispatch_text == "from Alice"

    alice = _row(engine, str(queued[0].delivery_id))
    bob = _row(engine, str(queued[1].delivery_id))
    assert alice["turn_id"] == alice_turn_id
    assert alice["state"] == "claimed"
    assert bob["turn_id"] is None
    assert bob["state"] == "queued"


def test_persisted_start_attempt_reaches_dispatch_context(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    captured: dict[str, object] = {}

    async def capture_run(_session_id, context, _text, **_kwargs):
        captured.update(context.platform_specific or {})

    manager._run = capture_run
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="start"),
            context=_context(),
        )
    )
    assert admitted.turn_id is not None
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, admitted.turn_id)
    assert turn is not None
    assert captured["delivery_start_attempt_id"] == turn["start_attempt_id"]


def test_dispatch_uses_current_session_route_without_mutating_delivery_provenance(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    captured: dict[str, object] = {}
    current = _context()
    current.platform_specific.update(
        {
            "vibe_agent_id": "agent-stable",
            "vibe_agent_name": "_pm-archived",
            "agent_session_target": {
                "id": "ses_fsm",
                "agent_id": "agent-stable",
                "agent_name": "_pm-archived",
                "agent_backend": "codex",
            },
        }
    )
    manager._build_context = lambda _session_id: current

    async def capture_run(_session_id, context, _text, **_kwargs):
        captured.update(context.platform_specific or {})

    manager._run = capture_run
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="scheduled",
                metadata={
                    SCHEDULED_PROVENANCE_KEY: {
                        "message_id": "scheduled:old",
                        "platform_specific": {
                            "vibe_agent_id": "agent-stable",
                            "vibe_agent_name": "pm",
                            "scheduled_target_agent_name": "pm",
                            "agent_session_target": {
                                "agent_id": "agent-stable",
                                "agent_name": "pm",
                            },
                            "task_trigger_kind": "agent_run",
                            "task_definition_id": "run-definition",
                        },
                    }
                },
            ),
            context=_context(),
        )
    )

    assert admitted.turn_id is not None
    assert captured["vibe_agent_id"] == "agent-stable"
    assert captured["vibe_agent_name"] == "_pm-archived"
    assert captured["agent_session_target"]["agent_name"] == "_pm-archived"
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, str(admitted.delivery_id))
    assert delivery is not None
    provenance = json.loads(delivery["snapshot_json"])["metadata_json"]
    persisted = json.loads(provenance)[SCHEDULED_PROVENANCE_KEY]["platform_specific"]
    assert persisted["vibe_agent_name"] == "pm"


def test_terminal_evidence_cannot_accept_an_unproven_native_start(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    first = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="unproven"),
            context=_context(),
        )
    )
    second = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="queued"),
            context=_context(),
        )
    )
    assert first.turn_id is not None
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )

    terminal = manager._terminalize_durable_turn(
        first.turn_id,
        "failed",
        settled_by="terminal_result",
        evidence_kind="terminal_result_without_start_receipt",
    )

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, first.turn_id)
        first_delivery = delivery_store.get_delivery(conn, str(first.delivery_id))
        second_delivery = delivery_store.get_delivery(conn, str(second.delivery_id))
        message_ids = list(conn.execute(select(messages.c.id)).scalars())
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one()
    assert terminal["changed"] is False
    assert terminal["reason"] == "start_acceptance_unproven"
    assert turn is not None and turn["state"] == "starting"
    assert first_delivery is not None and first_delivery["state"] == "claimed"
    assert second_delivery is not None and second_delivery["state"] == "queued"
    assert message_ids == []
    assert status == "running"


def test_terminal_transaction_claims_fifo_and_projects_running_atomically(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager, text="active"))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="next"),
            context=_context(),
        )
    )
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )

    terminal = manager._terminalize_durable_turn(
        turn_id,
        "completed",
        settled_by="test",
        evidence_kind="test_terminal",
    )

    assert terminal["successor_turn_id"]
    with engine.connect() as conn:
        successor = delivery_store.active_turn(conn, "ses_fsm")
        queued_row = delivery_store.get_delivery(conn, str(queued.delivery_id))
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one()
    assert successor is not None
    assert successor["id"] == terminal["successor_turn_id"]
    assert queued_row is not None and queued_row["turn_id"] == successor["id"]
    assert status == "running"


def test_post_native_terminal_storage_failure_retains_owner_and_running_projection(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, context = asyncio.run(_activate(manager, text="active"))
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )

    def fail_terminal_write(*_args, **_kwargs):
        raise RuntimeError("injected terminal storage failure")

    monkeypatch.setattr(manager, "_write_terminal_snapshot", fail_terminal_write)
    manager.on_terminal_result(context, is_error=False)
    manager.on_terminal_delivery_complete(context)

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one()
    assert turn is not None and turn["state"] == "active"
    assert status == "running"


def test_turn_state_repairs_only_an_exact_ownerless_running_projection(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )

    state = manager.turn_state("ses_fsm")

    assert state["in_flight"] is False
    assert state["recovered_agent_status"] is True
    with engine.connect() as conn:
        assert conn.execute(
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one() == "idle"


def test_older_reserved_submission_fences_later_queue_drain(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        for delivery_id, state, submitted_at in (
            ("msg_reserved_first", "reserved", "2026-08-01T00:00:01Z"),
            ("msg_queued_second", "queued", "2026-08-01T00:00:02Z"),
        ):
            delivery_store.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id="ses_fsm",
                priority="p3",
                state=state,
                snapshot=delivery_store.message_snapshot(
                    scope_id=None,
                    session_id="ses_fsm",
                    platform="avibe",
                    author="user",
                    source="user",
                    message_type="user",
                    text=delivery_id,
                ),
                dispatch_text=delivery_id,
                now=submitted_at,
            )

    assert not asyncio.run(manager.drain_delivery_queue("ses_fsm"))
    assert starts == []
    assert _row(engine, "msg_reserved_first")["state"] == "reserved"
    assert _row(engine, "msg_queued_second")["state"] == "queued"


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
    assert outcome.state == "claimed"
    assert delivery["turn_id"] == outcome.turn_id
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
    assert steer_calls == ["head-one\nhead-two"]
    queued = [row for row in _rows(engine) if row["state"] == "queued"]
    assert queued == []
    assert _row(engine, str(results[0].delivery_id))["turn_id"] in {None, turn_id}


def test_empty_p1_refuses_a_requested_delivery_that_is_not_the_head(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    asyncio.run(_activate(manager))
    queued = [
        asyncio.run(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p3", content=text),
                context=_context(),
            )
        )
        for text in ("head-one", "head-two")
    ]
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content=None,
                expected_delivery_id=str(queued[1].delivery_id),
            ),
            context=_context(),
        )
    )

    assert result.state == "refused"
    assert result.reason == "stale_head"
    assert result.delivery_id == queued[1].delivery_id
    manager._steer.assert_not_awaited()
    assert [row["state"] for row in _rows(engine) if row["id"] in {
        queued[0].delivery_id,
        queued[1].delivery_id,
    }] == ["queued", "queued"]


def test_run_cancellation_retires_every_pre_write_delivery_state(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    delivery_ids = [delivery_store.new_delivery_id() for _ in range(2)]
    with engine.begin() as conn:
        rows = []
        for index, delivery_id in enumerate(delivery_ids):
            rows.append(
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
                        author="harness",
                        source="harness",
                        message_type="harness",
                        text=f"cancel-{index}",
                    ),
                    dispatch_text=f"cancel-{index}",
                )
            )
        pending = delivery_store.open_pending_steer_batch(
            conn,
            deliveries=[rows[1]],
            turn_id=turn_id,
            attempt_id=delivery_store.new_attempt_id(),
        )
        assert len(pending) == 1
        assert delivery_store.retire_for_run_cancellation(
            conn, "ses_fsm", delivery_ids[0]
        )
        assert delivery_store.retire_for_run_cancellation(
            conn, "ses_fsm", delivery_ids[1]
        )

    with engine.connect() as conn:
        retired = [delivery_store.get_delivery(conn, delivery_id) for delivery_id in delivery_ids]
    assert [row["state"] for row in retired] == ["retired", "retired"]
    assert all(row["current_attempt_id"] is None for row in retired)
    assert all(row["current_target_turn_id"] is None for row in retired)


@pytest.mark.anyio
async def test_empty_p1_accepted_agent_run_joins_active_turn_ownership(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, active_context = await _activate(manager)
    holder = asyncio.Event()
    holder_task = asyncio.create_task(holder.wait())
    manager.in_flight["ses_fsm"] = Turn(
        task=holder_task,
        context=active_context,
        logical_turn_id=turn_id,
    )
    sink_done = asyncio.Event()
    manager.register_turn_sink(
        "avibe::ses_fsm",
        on_chunk=AsyncMock(),
        done_event=sink_done,
        turn_token=turn_id,
        context=active_context,
    )
    run_id = "run-steered-fifo-head"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                definition_id=None,
                run_type="agent_run",
                status="running",
                cancel_requested=0,
                session_id="ses_fsm",
                created_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    queued = await manager.deliver(
        DeliveryRequest(
            session_id="ses_fsm",
            priority="p3",
            content="queued Agent Run prompt",
            source="harness",
            author="harness",
            message_type="harness",
            native_message_id=f"agent_run:{run_id}",
            metadata={
                "scheduled_provenance": {
                    "task_execution_id": run_id,
                    "platform_specific": {
                        "task_trigger_kind": "agent_run",
                        "task_execution_id": run_id,
                    },
                }
            },
        ),
        context=_context(),
    )
    assert queued.state == "queued"
    with engine.begin() as conn:
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(delivery_id=queued.delivery_id)
        )
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    accepted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
        context=_context(),
    )

    assert accepted.state == "accepted"
    with engine.connect() as conn:
        run = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id)).mappings().one()
    assert run["status"] == "running"
    assert run["delivery_id"] == queued.delivery_id
    assert active_context.platform_specific["accepted_agent_run_ids"] == [run_id]
    assert manager.get_turn_sink("avibe::ses_fsm")["accepted_agent_run_ids"] == [run_id]

    manager.in_flight.pop("ses_fsm", None)
    manager.pop_turn_sink("avibe::ses_fsm", sink_done)
    holder_task.cancel()
    await asyncio.gather(holder_task, return_exceptions=True)


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
        asyncio.run(
            _activate(
                replacement_manager,
                text="replacement-owner",
                priority="p1",
            )
        )
        release.set()
        outcome = future.result(timeout=5)

    assert outcome.state == "refused"
    assert outcome.reason == "stale_head"
    assert _row(engine, str(queued.delivery_id))["state"] == "accepted"
    assert steer_calls == []


def test_p1_steer_uses_persisted_dispatch_text(managers, monkeypatch) -> None:
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

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "core.inbox_events.bus.publish",
        lambda event, payload: published.append((event, payload)),
    )
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
    assert row["turn_id"] == turn_id
    assert ("queue.updated", {"session_id": "ses_fsm"}) in published
    with engine.connect() as conn:
        materialized = conn.execute(
            select(messages.c.content_text).where(messages.c.id == delivery_id)
        ).scalar_one()
    assert materialized == "display content"


def test_lost_accepted_receipt_materializes_from_exact_restart_evidence(
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
    original = delivery_store.materialize_steer_acceptance

    def lose_receipt(*args, **kwargs):
        raise OSError("simulated receipt fsync loss")

    monkeypatch.setattr(delivery_store, "materialize_steer_acceptance", lose_receipt)
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="accepted once"),
            context=_context(),
        )
    )
    monkeypatch.setattr(delivery_store, "materialize_steer_acceptance", original)
    restarted._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")

    async def reconcile(_backend, request):
        assert request.attempt_id
        assert request.expected_logical_turn_id == turn_id
        return steer_result(
            SteerOutcome.ACCEPTED,
            reason="native_attempt_found",
            native_message_id=request.attempt_id,
        )

    restarted._reconcile_steer_attempt = reconcile
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(outcome.delivery_id))
    assert calls == 1
    assert row["state"] == "accepted"
    assert row["turn_id"] == turn_id
    assert row["current_attempt_id"] is None
    with engine.connect() as conn:
        message = conn.execute(
            select(messages).where(messages.c.id == row["message_id"])
        ).mappings().one()
    assert message["content_text"] == "accepted once"


def test_missing_restart_evidence_keeps_unknown_without_resteer(
    managers,
    monkeypatch,
) -> None:
    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    first._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    steer_calls = 0
    reconciliation_calls = 0

    async def accepted(_backend, _request):
        nonlocal steer_calls
        steer_calls += 1
        return steer_result(SteerOutcome.ACCEPTED)

    first._steer = accepted
    original = delivery_store.materialize_steer_acceptance

    def lose_receipt(*args, **kwargs):
        raise OSError("simulated receipt fsync loss")

    monkeypatch.setattr(delivery_store, "materialize_steer_acceptance", lose_receipt)
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="still unknown"),
            context=_context(),
        )
    )
    monkeypatch.setattr(delivery_store, "materialize_steer_acceptance", original)
    restarted._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")

    async def reconcile(_backend, request):
        nonlocal reconciliation_calls
        reconciliation_calls += 1
        assert request.expected_logical_turn_id == turn_id
        return steer_result(SteerOutcome.UNKNOWN, reason="evidence_unavailable")

    restarted._reconcile_steer_attempt = reconcile
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(outcome.delivery_id))
    assert steer_calls == 1
    assert reconciliation_calls == 1
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
    assert row["state"] == "claimed"
    matching_starts = [item for item in starts if item[1] == "fallback"]
    assert len(matching_starts) == 1
    assert matching_starts[0][0] == row["turn_id"]


def test_definitive_p1_refusal_requeues_behind_existing_fifo_head(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    older = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="older backlog"),
            context=_context(),
        )
    )
    with engine.begin() as conn:
        assert delivery_store.set_queue_hold(conn, "ses_fsm", held=True)
    entered = threading.Event()
    release = threading.Event()

    async def refused(_backend, _request):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return steer_result(SteerOutcome.REFUSED, reason="not_steerable")

    manager._steer = refused

    async def race():
        pending = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content="fallback"),
                context=_context(),
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        assert await manager.terminalize_turn(turn_id)
        with engine.begin() as conn:
            assert delivery_store.set_queue_hold(conn, "ses_fsm", held=False)
        release.set()
        return await pending

    fallback = asyncio.run(race())
    older_row = _row(engine, str(older.delivery_id))
    fallback_row = _row(engine, str(fallback.delivery_id))
    assert older_row["state"] == "claimed"
    assert fallback_row["state"] == "claimed"
    assert fallback_row["turn_id"] == older_row["turn_id"]
    assert [text for _turn, text in starts].count("older backlog\nfallback") == 1


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


def test_empty_p0_establishes_durable_hold_before_stop(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        try:
            result = await manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p0", content=None),
                context=context,
            )
            assert result.state == "waiting_terminal"
            with engine.connect() as conn:
                assert delivery_store.queue_is_held(conn, "ses_fsm") is True
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    asyncio.run(run())


def test_content_p0_preserves_open_hold_and_claims_successor_once(managers) -> None:
    """MESSAGE-DELIVERY-014: control, terminal, and restart converge once."""

    manager, restarted, engine, _engine_b, starts = managers

    async def run() -> tuple[str, str, str]:
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
    assert delivery["state"] == "claimed"
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm") is False


def test_empty_p0_supersedes_in_flight_content_replacement(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, str, str, str]:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_stop(_stop_context):
            entered.set()
            await release.wait()
            return True

        manager.controller.command_handler.handle_stop = AsyncMock(side_effect=delayed_stop)
        replacement_task = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p0", content="replacement"),
                context=_context(),
            )
        )
        await entered.wait()
        with engine.connect() as conn:
            replacement_control = delivery_store.get_turn(conn, turn_id)
        assert replacement_control is not None
        successor_turn_id = str(replacement_control["control_successor_turn_id"] or "")
        replacement_delivery_id = str(
            replacement_control["control_successor_delivery_id"] or ""
        )
        run_id = "run-replacement-superseded-by-stop"
        now = "2026-08-01T00:00:00Z"
        with engine.begin() as conn:
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    definition_id=None,
                    run_type="agent_run",
                    status="running",
                    cancel_requested=0,
                    session_id="ses_fsm",
                    delivery_id=replacement_delivery_id,
                    created_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
            )
        stopped = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content=None),
            context=context,
        )
        release.set()
        await replacement_task
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        assert stopped.reason == "joined_existing_interrupt"
        return turn_id, successor_turn_id, replacement_delivery_id, run_id

    turn_id, successor_turn_id, replacement_delivery_id, run_id = asyncio.run(run())
    with engine.connect() as conn:
        target = delivery_store.get_turn(conn, turn_id)
        successor = delivery_store.get_turn(conn, successor_turn_id)
        run_row = conn.execute(
            select(agent_runs.c.status, agent_runs.c.cancel_requested).where(
                agent_runs.c.id == run_id
            )
        ).one()
        assert delivery_store.queue_is_held(conn, "ses_fsm") is True
    assert target is not None and target["control_mode"] == "stop_only"
    assert target["control_successor_turn_id"] is None
    assert target["control_successor_delivery_id"] is None
    assert successor is not None and successor["terminal_outcome"] == "not_written"
    assert _row(engine, replacement_delivery_id)["state"] == "retired"
    assert run_row == ("canceled", 1)
    assert manager.controller.command_handler.handle_stop.await_count == 1


def test_recovery_consumes_persisted_not_active_stop_receipt(managers) -> None:
    manager, restarted, engine, _engine_b, starts = managers

    async def run() -> tuple[str, str]:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )

        async def not_active(stop_context):
            stop_context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        manager.controller.command_handler.handle_stop = AsyncMock(
            side_effect=not_active
        )
        original_terminalize = manager._terminalize_durable_turn

        def crash_after_receipt(*args, **kwargs):
            if kwargs.get("evidence_kind") == "stop_not_active":
                raise OSError("crash after Stop receipt commit")
            return original_terminalize(*args, **kwargs)

        manager._terminalize_durable_turn = crash_after_receipt
        with pytest.raises(OSError, match="Stop receipt"):
            await manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p0",
                    content="restart successor",
                ),
                context=_context(),
            )
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        assert old is not None
        successor_turn_id = str(old["control_successor_turn_id"])
        assert old["control_receipt_outcome"] == "not_active"

        restarted._active_identity = lambda *_args: None
        await restarted.recover_durable_delivery_state()
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return turn_id, successor_turn_id

    old_turn_id, successor_turn_id = asyncio.run(run())
    with engine.connect() as conn:
        old = delivery_store.get_turn(conn, old_turn_id)
        successor = delivery_store.get_turn(conn, successor_turn_id)
    assert old is not None and old["state"] == "terminal"
    assert old["terminal_outcome"] == "canceled"
    assert successor is not None and successor["state"] == "starting"
    assert [turn for turn, text in starts if text == "restart successor"] == [
        successor_turn_id
    ]


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

    assert sorted(result.state for result in outcomes) == ["claimed", "queued"]
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
    assert asyncio.run(other.drain_delivery_queue("ses_fsm")) is False
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


def test_start_write_ambiguity_replays_once_after_restart(managers) -> None:
    """MESSAGE-DELIVERY-017: availability wins after an unresolvable start."""

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
    assert row["state"] == "claimed"
    assert len(starts) == 2
    assert starts[0][1] == "once"
    assert starts[1][1].endswith("once")
    assert "may have been delivered before restart" in starts[1][1]
    with engine.connect() as conn:
        assert conn.execute(select(messages.c.id)).all() == []
        turns = conn.execute(
            select(session_turns)
            .where(session_turns.c.initial_delivery_id == outcome.delivery_id)
            .order_by(session_turns.c.created_at, session_turns.c.id)
        ).mappings().all()
    assert len(turns) == 2
    assert turns[0]["state"] == "terminal"
    assert turns[0]["terminal_outcome"] == "failed"
    assert turns[0]["start_receipt_outcome"] == "unknown"
    assert turns[1]["state"] == "starting"


def test_accepted_codex_turn_without_runtime_settles_and_releases_queue(managers) -> None:
    first, restarted, engine, _engine_b, starts = managers
    turn_id, _context_value = asyncio.run(_activate(first, text="accepted before restart"))
    queued = asyncio.run(
        first.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="continue after restart",
            ),
            context=_context(),
        )
    )
    starts.clear()
    restarted._active_identity = lambda *_args: None

    asyncio.run(
        restarted.recover_durable_delivery_state(
            "ses_fsm",
            service_restart=True,
        )
    )

    with engine.connect() as conn:
        settled = delivery_store.get_turn(conn, turn_id)
        accepted = delivery_store.delivery_for_turn(conn, turn_id)
    assert settled is not None and settled["state"] == "terminal"
    assert settled["terminal_outcome"] == "failed"
    assert settled["terminal_evidence_kind"] == "restart_runtime_missing"
    assert accepted is not None and accepted["state"] == "accepted"
    assert _row(engine, str(queued.delivery_id))["state"] == "claimed"
    assert [text for _started_turn, text in starts] == ["continue after restart"]


def test_accepted_opencode_turn_without_restored_identity_stays_live(managers) -> None:
    first, restarted, engine, _engine_b, starts = managers
    turn_id, _context_value = asyncio.run(_activate(first, text="restorable runtime"))
    queued = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="wait"),
            context=_context(),
        )
    )
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_backend="opencode", agent_name="opencode")
        )
        conn.execute(
            update(session_turns)
            .where(session_turns.c.id == turn_id)
            .values(backend="opencode")
        )
    starts.clear()
    restarted._active_identity = lambda *_args: None

    asyncio.run(
        restarted.recover_durable_delivery_state(
            "ses_fsm",
            service_restart=True,
        )
    )

    with engine.connect() as conn:
        retained = delivery_store.get_turn(conn, turn_id)
    assert retained is not None and retained["state"] == "active"
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"
    assert starts == []


def test_second_unknown_start_retires_delivery_and_unblocks_fifo(managers) -> None:
    first, restarted, engine, _engine_b, starts = managers
    settlements: list[tuple[list[str], str]] = []
    settlement_service = SimpleNamespace(
        settle_agent_runs_without_result=lambda run_ids, *, settled_by: settlements.append(
            (list(run_ids), settled_by)
        )
    )
    first.controller.scheduled_task_service = settlement_service
    restarted.controller.scheduled_task_service = settlement_service

    async def ambiguous(_session_id, _context_value, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))
        raise OSError("connection lost after native write")

    first._run = ambiguous
    restarted._run = ambiguous
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="retry once"),
            context=_context(),
        )
    )
    run_id = "run-unknown-start-retry"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                definition_id=None,
                run_type="agent",
                status="running",
                cancel_requested=0,
                session_id="ses_fsm",
                delivery_id=str(outcome.delivery_id),
                created_at=now,
                started_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )
    restarted._active_identity = lambda *_args: None
    asyncio.run(restarted.recover_durable_delivery_state())
    assert len(starts) == 2

    with engine.begin() as conn:
        following = delivery_store.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="continue queue",
        )

    async def succeeds(_session_id, _context_value, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))

    first._run = succeeds
    first._active_identity = lambda *_args: None
    asyncio.run(first.recover_durable_delivery_state())

    retired = _row(engine, str(outcome.delivery_id))
    assert retired["state"] == "retired"
    history = json.loads(retired["delivery_history_json"])["events"]
    assert [
        event["outcome"]
        for event in history
        if event["kind"] == "start" and event["outcome"].startswith("restart_")
    ] == [
        "restart_replayed",
        "restart_retry_exhausted",
    ]
    assert _row(engine, str(following["id"]))["state"] == "claimed"
    assert starts[-1][1] == "continue queue"
    assert settlements == [([run_id], "no_terminal_result")]


def test_exact_missing_start_attempt_requeues_only_its_own_delivery(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="retry exact"),
            context=_context(),
        )
    )
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert turn is not None
    attempt_id = str(turn["start_attempt_id"])

    assert not manager.reconcile_start_attempt_not_written(
        str(admitted.turn_id),
        "different-attempt",
        backend="opencode",
    )
    with engine.connect() as conn:
        still_starting = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert still_starting is not None and still_starting["state"] == "starting"

    assert manager.reconcile_start_attempt_not_written(
        str(admitted.turn_id),
        attempt_id,
        backend="opencode",
    )
    assert _row(engine, str(admitted.delivery_id))["state"] == "queued"
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert terminal is not None
    assert terminal["terminal_outcome"] == "not_written"


def test_pre_dispatch_hydration_failure_is_definitively_recoverable(managers) -> None:
    first, restarted, engine, _engine_b, starts = managers

    def fail_before_dispatch(*_args, **_kwargs):
        raise OSError("attachment lookup failed before dispatch")

    first._hydrate_delivery_context = fail_before_dispatch
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="retry safely"),
            context=_context(),
        )
    )

    row = _row(engine, str(admitted.delivery_id))
    assert row["state"] == "queued"
    with engine.connect() as conn:
        failed_turn = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert failed_turn is not None
    assert failed_turn["terminal_outcome"] == "not_written"
    assert starts == []

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm"))

    assert [text for _turn_id, text in starts] == ["retry safely"]
    assert _row(engine, str(admitted.delivery_id))["state"] == "claimed"


def test_submit_reports_the_requeued_state_after_prewrite_failure(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, starts = managers
    published: list[tuple[str, dict]] = []

    def fail_before_dispatch(*_args, **_kwargs):
        raise OSError("attachment lookup failed before dispatch")

    manager._hydrate_delivery_context = fail_before_dispatch
    monkeypatch.setattr(
        "core.inbox_events.bus.publish",
        lambda event, payload: published.append((event, payload)),
    )

    result = asyncio.run(
        manager.submit(
            "ses_fsm",
            _context(),
            "retry exact scheduled work",
            source=SOURCE_SCHEDULED,
        )
    )

    assert result.route == "enqueued"
    assert result.queue_persisted is True
    assert starts == []
    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0]["state"] == "queued"
    assert ("queue.updated", {"session_id": "ses_fsm"}) in published


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
                status="running",
                cancel_requested=1,
                session_id="ses_fsm",
                created_at=now,
                completed_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
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
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="codex",
            deliveries=[delivery],
            dispatch_text="must not run",
            attempt_id=attempt_id,
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(delivery_id=delivery_id)
        )

    assert asyncio.run(manager._start_persisted_turn(turn_id, context=_context())) is False
    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None and turn["terminal_outcome"] == "not_written"


def test_terminal_run_wins_after_turn_launch_before_runner_dispatch(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    native_dispatch = AsyncMock()
    monkeypatch.setattr("core.session_turns.dispatch_turn_with_outcome", native_dispatch)
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    run_id = "run-failed-after-turn-launch"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="harness",
                source="harness",
                message_type="harness",
                text="must still not run",
            ),
            dispatch_text="must still not run",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="codex",
            deliveries=[delivery],
            dispatch_text="must still not run",
        )
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                run_type="task_run",
                status="failed",
                session_id="ses_fsm",
                delivery_id=delivery_id,
                cancel_requested=0,
                error="task result was not recorded",
                created_at=now,
                completed_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    async def run() -> None:
        await manager._run(
            "ses_fsm",
            _context(),
            "must still not run",
            source="scheduled",
            logical_turn_id=turn_id,
            delivery_id=delivery_id,
            durable_preallocated=True,
        )
        await manager.in_flight["ses_fsm"].task

    asyncio.run(run())

    native_dispatch.assert_not_awaited()
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["terminal_outcome"] == "not_written"
    assert turn["settled_by"] == "terminal_run"


def test_definite_handler_prewrite_exit_requeues_through_terminal_boundary(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    with engine.begin() as conn:
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="retry after backend is enabled",
            ),
            dispatch_text="retry after backend is enabled",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="opencode",
            deliveries=[delivery],
            dispatch_text="retry after backend is enabled",
        )

    async def definite_prewrite_exit(*_args, **_kwargs):
        return TurnDispatchOutcome(
            error="agent 'opencode' is not available",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            backend_dispatch_attempted=False,
        )

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        definite_prewrite_exit,
    )

    async def run() -> None:
        await manager._run(
            "ses_fsm",
            _context(),
            "retry after backend is enabled",
            logical_turn_id=turn_id,
            delivery_id=delivery_id,
            durable_preallocated=True,
        )
        await manager.in_flight["ses_fsm"].task

    asyncio.run(run())

    assert _row(engine, delivery_id)["state"] == "queued"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["terminal_outcome"] == "not_written"
    assert turn["settled_by"] == "no_terminal_result"


def test_definite_handler_prewrite_exception_requeues_through_terminal_boundary(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    with engine.begin() as conn:
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="retry after terminal persistence failure",
            ),
            dispatch_text="retry after terminal persistence failure",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="opencode",
            deliveries=[delivery],
            dispatch_text="retry after terminal persistence failure",
        )

    async def definite_prewrite_exception(_controller, context, *_args, **_kwargs):
        set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        raise OSError("terminal error persistence failed")

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        definite_prewrite_exception,
    )

    async def run() -> None:
        await manager._run(
            "ses_fsm",
            _context(),
            "retry after terminal persistence failure",
            logical_turn_id=turn_id,
            delivery_id=delivery_id,
            durable_preallocated=True,
        )
        await manager.in_flight["ses_fsm"].task

    asyncio.run(run())

    assert _row(engine, delivery_id)["state"] == "queued"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["terminal_outcome"] == "not_written"
    assert turn["start_receipt_outcome"] == "not_written"


def test_forced_backend_refresh_fails_unresolved_start_instead_of_blocking(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    with engine.begin() as conn:
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="ambiguous during forced refresh",
            ),
            dispatch_text="ambiguous during forced refresh",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="codex",
            deliveries=[delivery],
            dispatch_text="ambiguous during forced refresh",
        )
    manager.begin_backend_drain("codex")

    released = asyncio.run(
        manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={"ses_fsm"},
        )
    )

    assert released == 1
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(agent_sessions.c.id == "ses_fsm")
        ).scalar_one()
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "failed"
    assert turn["terminal_evidence_kind"] == "backend_refresh_start_failed"
    assert status == "failed"


def test_restore_registration_failure_terminalizes_exact_start(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    delivery_id = delivery_store.new_delivery_id()
    turn_id = delivery_store.new_turn_id()
    with engine.begin() as conn:
        delivery = delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="restore registration failure",
            ),
            dispatch_text="restore registration failure",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="opencode",
            deliveries=[delivery],
            dispatch_text="restore registration failure",
        )

    assert manager.fail_restored_backend_turn(
        turn_id,
        backend="opencode",
        reason="poll_registration_failed",
    )

    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        status = conn.execute(
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one()
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "failed"
    assert turn["terminal_evidence_kind"] == "backend_restore_failed"
    assert status == "failed"


def test_materialized_message_preserves_submission_and_acceptance_times(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager, text="ordered input"))
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        assert turn is not None
        delivery = conn.execute(
            select(message_deliveries).where(
                message_deliveries.c.id == turn["initial_delivery_id"]
            )
        ).mappings().one()
        message = conn.execute(
            select(messages).where(messages.c.id == delivery["id"])
        ).mappings().one()
    assert message["created_at"] == delivery["submitted_at"]
    assert message["delivered_at"] == delivery["materialized_at"]


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
    assert _row(engine, delivery_id)["state"] == "claimed"


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
    assert _row(engine, delivery_id)["state"] == "claimed"


def test_empty_reserved_submission_is_retired_without_native_dispatch(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
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
                text="   ",
                content={"attachments": []},
            ),
            dispatch_text="   ",
        )

    assert asyncio.run(manager.recover_durable_delivery_state()) == ["ses_fsm"]

    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"


def test_terminal_run_retires_its_exact_prewrite_delivery(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    now = "2026-08-01T00:00:00Z"
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
                author="harness",
                source="harness",
                message_type="harness",
                text="scheduled input",
            ),
            dispatch_text="scheduled input",
        )
        conn.execute(
            agent_runs.insert().values(
                id="run_terminal_stamp_refused",
                run_type="task_run",
                status="failed",
                session_id="ses_fsm",
                delivery_id=delivery_id,
                cancel_requested=0,
                error="task result was not recorded",
                created_at=now,
                completed_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    result = asyncio.run(
        manager.reconcile_terminal_run_delivery(
            "run_terminal_stamp_refused",
            session_id="ses_fsm",
        )
    )

    assert result == {"changed": True, "state": "retired"}
    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"


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
    assert asyncio.run(other.drain_delivery_queue("ses_fsm")) is False
    older_turn = _row(engine, next(row["id"] for row in _rows(engine) if row["dispatch_text"] == "older"))[
        "turn_id"
    ]
    assert older_turn
    older_context = _context()
    older_context.platform_specific["turn_token"] = str(older_turn)
    older_context.platform_specific["agent_runtime_turn_token"] = f"runtime-{older_turn}"
    other._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    other.on_native_start(
        older_context,
        backend="codex",
        runtime_key=f"runtime-key-{older_turn}",
        runtime_turn_id=f"runtime-{older_turn}",
    )
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
        assert admitted.state == "claimed"

    asyncio.run(run())
    rows = _rows(engine)
    assert next(row for row in rows if row["dispatch_text"] == "held-old")["state"] == "queued"
    assert [text for _, text in starts].count("new-idle") == 1
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm") is True


def test_open_backlog_starts_oldest_before_new_idle_p3(managers) -> None:
    """An open compatible FIFO segment is claimed as one native Turn."""

    manager, _other, engine, _engine_b, starts = managers
    started_contexts: list[dict[str, object]] = []

    async def capture_start(_session_id, context, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))
        started_contexts.append(dict(context.platform_specific or {}))

    manager._run = capture_start
    with engine.begin() as conn:
        older = delivery_store.enqueue_queued(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            text="older-drain-head",
            now="2026-07-31T23:59:00+00:00",
        )

    new_context = _context()
    new_context.platform_specific["new_submission_only"] = True
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="new-after-drain",
            ),
            context=new_context,
        )
    )

    assert admitted.state == "claimed"
    assert [text for _, text in starts] == ["older-drain-head\nnew-after-drain"]
    assert "new_submission_only" not in started_contexts[0]
    older_after = _row(engine, older["id"])
    new_after = _row(engine, str(admitted.delivery_id))
    assert older_after["state"] == "claimed"
    assert new_after["state"] == "claimed"
    assert new_after["turn_id"] == older_after["turn_id"]


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
    assert row["turn_id"] == turn_id
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        message = conn.execute(select(messages).where(messages.c.id == row["id"])).mappings().one()
    assert turn["state"] == "terminal"
    assert message["content_text"] == "late receipt"


@pytest.mark.parametrize("activity_blocks_settlement", [False, True])
def test_late_accepted_agent_run_settles_from_terminal_turn_snapshot(
    managers,
    activity_blocks_settlement: bool,
) -> None:
    from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    manager, _terminal_manager, engine, _engine_b, _starts = managers
    db_path = Path(str(engine.url.database))
    run_store = SQLiteBackgroundTaskStore(db_path)
    request_store = TaskExecutionStore(root=db_path.parent / "unused-file-store")
    request_store._sqlite = run_store
    scheduled = ScheduledTaskService.__new__(ScheduledTaskService)
    scheduled.controller = manager.controller
    scheduled.request_store = request_store
    scheduled._drain_dirty = False
    manager.controller.scheduled_task_service = scheduled
    manager.controller.agent_service.activities = SimpleNamespace(
        has_blocking_run_activity=lambda _run_id: activity_blocks_settlement,
    )

    async def run() -> tuple[str, str, str]:
        turn_id, active_context = await _activate(manager)
        run_id = "run-late-terminal-steer"
        now = "2026-08-01T00:00:00Z"
        with engine.begin() as conn:
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    definition_id=None,
                    run_type="agent_run",
                        status="running",
                    cancel_requested=0,
                    session_id="ses_fsm",
                    callback_session_id="ses_callback",
                    callback_status="pending",
                    created_at=now,
                    updated_at=now,
                        metadata_json="{}",
                )
            )
        queued = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="queued Agent Run prompt",
                source="harness",
                author="harness",
                message_type="harness",
                native_message_id=f"agent_run:{run_id}",
                metadata={
                    "scheduled_provenance": {
                        "task_execution_id": run_id,
                        "platform_specific": {
                            "task_trigger_kind": "agent_run",
                            "task_execution_id": run_id,
                        },
                    }
                },
            ),
            context=_context(),
        )
        assert queued.state == "queued"
        with engine.begin() as conn:
            conn.execute(
                update(agent_runs)
                .where(agent_runs.c.id == run_id)
                .values(delivery_id=queued.delivery_id)
            )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def accepted(_backend, _request):
            entered.set()
            await release.wait()
            return steer_result(SteerOutcome.ACCEPTED)

        manager._steer = accepted
        promoted = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
                context=_context(),
            )
        )
        await entered.wait()
        manager.on_terminal_result(
            active_context,
            is_error=False,
            terminal_evidence={
                "result_text": "immutable terminal body",
                "settles_run": True,
            },
        )
        manager.on_terminal_delivery_complete(active_context)
        release.set()
        accepted_delivery = await promoted
        assert accepted_delivery.state == "accepted"
        return turn_id, run_id, str(queued.delivery_id)

    try:
        turn_id, run_id, delivery_id = asyncio.run(run())
        with engine.connect() as conn:
            run_row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id)
            ).mappings().one()
            delivery = conn.execute(
                select(message_deliveries).where(
                    message_deliveries.c.id == delivery_id
                )
            ).mappings().one()
        assert delivery["turn_id"] == turn_id
        if activity_blocks_settlement:
            assert run_row["status"] == "running"
            payload = json.loads(run_row["result_payload_json"])
            assert payload["deferred_terminal_status"] == "succeeded"
            assert payload["deferred_terminal_result_text"] == "immutable terminal body"
            manager.controller.agent_service.activities.has_blocking_run_activity = (
                lambda _run_id: False
            )
            scheduled._recover_activity_lifecycle()
            assert request_store.get_run(run_id)["status"] == "succeeded"
        else:
            assert run_row["status"] == "succeeded"
            assert run_row["result_text"] == "immutable terminal body"
        assert run_row["callback_status"] == "pending"
        assert scheduled._drain_dirty is True
    finally:
        run_store.close()


def test_restart_settles_agent_run_after_late_acceptance_commit(managers) -> None:
    from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    manager, restarted, engine, _engine_b, _starts = managers

    async def settle_turn() -> str:
        turn_id, active_context = await _activate(manager)
        manager.on_terminal_result(
            active_context,
            is_error=False,
            terminal_evidence={
                "result_text": "recovered immutable terminal body",
                "settles_run": True,
            },
        )
        manager.on_terminal_delivery_complete(active_context)
        return turn_id

    turn_id = asyncio.run(settle_turn())
    run_id = "run-recovered-late-terminal-steer"
    delivery_id = delivery_store.new_delivery_id()
    attempt_id = delivery_store.new_attempt_id()
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                definition_id=None,
                run_type="agent_run",
                status="running",
                cancel_requested=0,
                session_id="ses_fsm",
                callback_session_id="ses_callback",
                callback_status="pending",
                created_at=now,
                started_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )
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
                author="harness",
                source="harness",
                message_type="harness",
                text="accepted before settlement crashed",
                native_message_id=f"agent_run:{run_id}",
                metadata={
                    "scheduled_provenance": {
                        "task_execution_id": run_id,
                        "platform_specific": {
                            "task_trigger_kind": "agent_run",
                            "task_execution_id": run_id,
                        },
                    }
                },
            ),
            dispatch_text="accepted before settlement crashed",
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(delivery_id=delivery_id)
        )
        delivery = delivery_store.get_delivery(conn, delivery_id)
        assert delivery is not None
        steering = delivery_store.open_steer_attempt(
            conn,
            delivery_id,
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=attempt_id,
            expected_native_turn_id=f"native-{turn_id}",
        )
        assert steering is not None
        assert delivery_store.materialize_acceptance(
            conn,
            delivery_id=delivery_id,
            expected_attempt_id=attempt_id,
            turn_id=turn_id,
            evidence={"kind": "steer_receipt", "receipt": {"outcome": "accepted"}},
        ) is not None

    db_path = Path(str(engine.url.database))
    run_store = SQLiteBackgroundTaskStore(db_path)
    request_store = TaskExecutionStore(root=db_path.parent / "unused-recovery-store")
    request_store._sqlite = run_store
    scheduled = ScheduledTaskService.__new__(ScheduledTaskService)
    scheduled.request_store = request_store
    scheduled._drain_dirty = False
    restarted.controller.scheduled_task_service = scheduled
    try:
        asyncio.run(restarted.recover_durable_delivery_state())
        with engine.connect() as conn:
            run_row = conn.execute(
                select(agent_runs).where(agent_runs.c.id == run_id)
            ).mappings().one()
        assert run_row["status"] == "succeeded"
        assert run_row["result_text"] == "recovered immutable terminal body"
        assert run_row["callback_status"] == "pending"
        assert scheduled._drain_dirty is True
    finally:
        run_store.close()


def test_terminal_turn_without_positive_result_evidence_keeps_late_run_unsettled(
    managers,
) -> None:
    from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    manager, _restarted, engine, _engine_b, _starts = managers
    run_id = "run-missing-terminal-result-evidence"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                definition_id=None,
                run_type="agent_run",
                status="running",
                cancel_requested=0,
                session_id="ses_fsm",
                callback_session_id="ses_callback",
                callback_status="pending",
                created_at=now,
                started_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    db_path = Path(str(engine.url.database))
    run_store = SQLiteBackgroundTaskStore(db_path)
    request_store = TaskExecutionStore(root=db_path.parent / "unused-no-evidence-store")
    request_store._sqlite = run_store
    scheduled = ScheduledTaskService.__new__(ScheduledTaskService)
    scheduled.controller = manager.controller
    scheduled.request_store = request_store
    scheduled._drain_dirty = False
    try:
        scheduled.settle_agent_runs_from_terminal_turn(
            [run_id],
            turn_id="turn-without-result-evidence",
            outcome="completed",
            settled_by="terminal_result",
            evidence_kind="agent_initiated_terminal",
            evidence={},
        )
        stored = request_store.get_run(run_id)
        assert stored is not None
        assert stored["status"] == "running"
        assert stored["result_text"] in {None, ""}
        assert scheduled._drain_dirty is False
    finally:
        run_store.close()


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
    with engine.begin() as conn:
        delivery_store.set_queue_hold(conn, "ses_fsm", held=True)
    assert asyncio.run(other.terminalize_turn(t1))
    t2, context2 = asyncio.run(
        _activate(other, text="new priority work", priority="p1")
    )
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
    assert row["turn_id"] == t2
    assert row["current_attempt_id"] is None


def test_stale_send_now_does_not_release_the_queue_hold(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    old_turn_id, _ = asyncio.run(_activate(manager, text="old turn"))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="held backlog"),
            context=_context(),
        )
    )
    with engine.connect() as conn:
        old_turn = delivery_store.get_turn(conn, old_turn_id)
    assert old_turn is not None
    with engine.begin() as conn:
        assert delivery_store.set_queue_hold(conn, "ses_fsm", held=True)
    assert asyncio.run(manager.terminalize_turn(old_turn_id))
    replacement_turn_id, replacement_context = asyncio.run(
        _activate(manager, text="replacement turn", priority="p1")
    )
    manager._observe_active_delivery_turn = lambda _session_id: (
        old_turn,
        (old_turn_id, f"native-{old_turn_id}"),
    )

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content=None,
                expected_delivery_id=str(queued.delivery_id),
            ),
            context=replacement_context,
        )
    )

    assert result.state == "refused"
    assert result.reason == "stale_turn"
    with engine.connect() as conn:
        assert delivery_store.queue_is_held(conn, "ses_fsm")
        current = delivery_store.active_turn(conn, "ses_fsm")
    assert current is not None and current["id"] == replacement_turn_id
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"


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
    assert _row(engine, str(admitted.delivery_id))["state"] == "claimed"
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
    delivery_id = delivery_store.new_delivery_id()
    run_id = "run-archived-steer-refused"

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
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p1",
                    content="archived",
                    delivery_id=delivery_id,
                ),
                context=_context(),
            )
        )
        await entered.wait()
        with engine.begin() as conn:
            now = "2026-08-01T00:00:00Z"
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    definition_id=None,
                    run_type="agent_run",
                    status="running",
                    cancel_requested=0,
                    session_id="ses_fsm",
                    delivery_id=delivery_id,
                    created_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
            )
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
        run_row = conn.execute(
            select(agent_runs.c.status, agent_runs.c.cancel_requested).where(
                agent_runs.c.id == run_id
            )
        ).one()
    assert run_row == ("canceled", 1)


def test_archive_retires_unstarted_successor_without_creating_message(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, str]:
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
        return turn_id, str(admitted.delivery_id)

    turn_id, delivery_id = asyncio.run(run())
    with engine.begin() as conn:
        workbench_sessions_service.archive_session(conn, "ses_fsm")
    assert asyncio.run(manager.terminalize_turn(turn_id, outcome="canceled"))
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
    manager.on_terminal_delivery_complete(context)
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
    manager.on_terminal_result(
        context,
        is_error=False,
        terminal_evidence={
            "result_text": "agent-initiated terminal body",
            "settles_run": True,
        },
    )
    sink["done_event"].set()
    for _ in range(4):
        await asyncio.sleep(0)
    with engine.connect() as conn:
        turn = delivery_store.get_turn(
            conn,
            str((context.platform_specific or {})["turn_token"]),
        )
    assert turn is not None
    assert json.loads(turn["terminal_evidence_json"]) == {
        "result_text": "agent-initiated terminal body",
        "settles_run": True,
    }
