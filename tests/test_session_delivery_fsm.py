from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, update

from avibe_memory.capture_adapter import EnabledMemoryAdapter
from core.handlers.message_handler import memory_turn_event
from core.memory_adapter import SessionArchived, SessionReset
from core.services.agent_steering import SteerOutcome, result as steer_result
from core.services.dispatch import TurnDispatchOutcome
from core.native_dispatch_phase import (
    DISPATCH_PHASE_ATTEMPTING,
    DISPATCH_PHASE_PREWRITE,
    prewrite_user_stop_requested,
    set_dispatch_phase,
)
from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_RESTARTED,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.processing_indicator import INTERRUPTED_REACTION_EMOJI
from core.runtime_activation import (
    RuntimeActivationRegistry,
    RuntimeActivationResolution,
)
from core.session_turns import (
    SCHEDULED_PROVENANCE_KEY,
    SOURCE_SCHEDULED,
    DeliveryRequest,
    DeliveryResult,
    SessionTurnManager,
    Turn,
    _collect_delivery_segment,
    _scheduled_merge_key,
)
from core.message_context import SCHEDULED_DISPATCH_METADATA_APPLIED_KEY
from core.handlers.message_handler import MessageHandler
from modules.im import MessageContext
from modules.im.base import FileAttachment
from storage import message_deliveries as delivery_store
from storage import messages_service
from storage import workbench_sessions_service
from storage.db import create_sqlite_engine
from storage.models import (
    agent_runs,
    agent_sessions,
    media_objects,
    message_deliveries,
    messages,
    metadata,
    session_turns,
    show_session_events,
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


def _memory_facts_controller():
    from core.controller import Controller

    controller = Controller.__new__(Controller)
    controller.config = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    controller.memory_runtime = SimpleNamespace(
        principal_for_user_key=lambda user_key: f"principal:{user_key}",
    )
    controller.platform_settings_managers = {}
    controller.get_cwd = lambda _context: None
    return controller


def _complete_capture_admission(context: MessageContext) -> None:
    """Guard that durable dispatch carries no lifecycle state in its JSON bag."""

    payload = context.platform_specific or {}
    assert "_turn_lifecycle_admission" not in payload
    assert "_turn_lifecycle_snapshot" not in payload
    json.dumps(payload)


def _agentless_context(session_id: str = "ses_fsm") -> MessageContext:
    return MessageContext(
        user_id="user",
        channel_id=session_id,
        platform="avibe",
        platform_specific={
            "workbench_session_id": session_id,
            "agent_session_id": session_id,
            "agent_session_target": {
                "id": session_id,
                "agent_id": None,
                "agent_name": None,
                "agent_backend": "",
                "agent_variant": "default",
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


@pytest.fixture(scope="module")
def _fsm_schema_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("fsm-schema") / "empty.sqlite"
    engine = create_sqlite_engine(path)
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture
def managers(tmp_path: Path, sqlite_db_factory, _fsm_schema_template):
    db_path = sqlite_db_factory(tmp_path / "fsm.sqlite", template=_fsm_schema_template)
    engine_a = create_sqlite_engine(db_path)
    engine_b = create_sqlite_engine(db_path)
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
        _complete_capture_admission(_context_value)

    manager_a._run = fake_run
    manager_b._run = fake_run
    yield manager_a, manager_b, engine_a, engine_b, starts
    engine_a.dispose()
    engine_b.dispose()


def test_fsm_template_matches_real_empty_metadata(tmp_path, _fsm_schema_template, sqlite_db_factory):
    reference = tmp_path / "reference.sqlite"
    engine = create_sqlite_engine(reference)
    try:
        metadata.create_all(engine)
        with engine.connect() as connection:
            assert all(connection.execute(select(table)).first() is None for table in metadata.tables.values())
    finally:
        engine.dispose()
    copied = sqlite_db_factory(tmp_path / "copied.sqlite", template=_fsm_schema_template)
    with closing(sqlite3.connect(reference)) as fresh, closing(sqlite3.connect(copied)) as clone:
        assert list(fresh.iterdump()) == list(clone.iterdump())
        for pragma in ("journal_mode", "user_version", "application_id"):
            assert fresh.execute(f"PRAGMA {pragma}").fetchall() == clone.execute(f"PRAGMA {pragma}").fetchall()
        assert clone.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.anyio
async def test_completed_memory_lifecycle_state_does_not_accumulate(managers) -> None:
    """Scenario: MEMORY-INDEP-005.

    Settled optional Memory fences must not retain every historical session.
    """

    manager, _other, _engine, _engine_b, _starts = managers
    for index in range(256):
        session_id = f"settled-memory-session-{index}"
        snapshot = manager.snapshot_session_lifecycle(session_id)
        admission = await manager.acquire_lifecycle_admission(session_id)
        admission.release()

        async def reset_session() -> str:
            return "reset"

        assert await manager.run_session_lifecycle(
            session_id,
            reset_session,
        ) == "reset"
        assert not manager.session_lifecycle_snapshot_matches(
            session_id,
            snapshot,
        )

    del admission, snapshot
    gc.collect()
    assert len(manager._session_lifecycle_states) == 0


@pytest.mark.anyio
async def test_session_lifecycle_invalidates_snapshot_after_operation(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    snapshot = manager.snapshot_session_lifecycle("ses_fsm")

    async def lifecycle_operation() -> str:
        assert manager.session_lifecycle_snapshot_matches("ses_fsm", snapshot)
        return "reset"

    assert await manager.run_session_lifecycle(
        "ses_fsm",
        lifecycle_operation,
    ) == "reset"
    assert not manager.session_lifecycle_snapshot_matches("ses_fsm", snapshot)


@pytest.mark.anyio
async def test_failed_session_lifecycle_preserves_sampled_epoch(managers) -> None:
    """MEMORY-IM-ATTACH-001: failed reset preserves an in-flight turn epoch."""

    manager, _other, _engine, _engine_b, _starts = managers
    sampled_snapshot = manager.snapshot_session_lifecycle("ses_fsm")

    async def lifecycle_operation() -> str:
        raise RuntimeError("reset failed")

    with pytest.raises(RuntimeError, match="reset failed"):
        await manager.run_session_lifecycle("ses_fsm", lifecycle_operation)

    admission = await manager.acquire_lifecycle_admission("ses_fsm")
    try:
        assert manager.session_lifecycle_snapshot_matches(
            "ses_fsm",
            sampled_snapshot,
        )
    finally:
        admission.release()


@pytest.mark.anyio
async def test_hung_memory_capture_does_not_fence_next_turn_or_destructive_ops(
    managers,
) -> None:
    """Scenario: MEMORY-INDEP-001."""

    manager, _other, _engine, _engine_b, starts = managers
    capture_started = asyncio.Event()

    class Module:
        def __init__(self) -> None:
            self.capacities: list[SimpleNamespace] = []
            self.reservations: list[SimpleNamespace] = []
            self.barriers: list[str] = []

        def reserve_capture_capacity(self) -> object:
            capacity = SimpleNamespace(active=True)
            self.capacities.append(capacity)
            return capacity

        def release_capture_capacity(self, capacity: object) -> None:
            capacity.active = False

        def reserve_capture_admission(self, **_scope: object) -> object:
            reservation = SimpleNamespace(active=True)
            self.reservations.append(reservation)
            return reservation

        def cancel_capture_reservation(self, reservation: object) -> None:
            reservation.active = False

        @asynccontextmanager
        async def capture_admission(self, **_options: object):
            yield object()

        async def capture(self, _request: object, **_options: object) -> object:
            capture_started.set()
            await asyncio.Event().wait()

        def offer_barrier(self, session_id: str) -> object:
            self.barriers.append(session_id)
            return "queued"

        async def wait_writer_idle_for_tests(self, **_options: object) -> None:
            return None

    class Principals:
        def principal_for_user_key(self, _user_key: str) -> str:
            return "u-11111111111111111111111111111111"

    module = Module()
    adapter = EnabledMemoryAdapter(
        module=module,
        principals=Principals(),
        is_enabled_user=lambda _platform, _user_id: True,
        lifecycle_snapshot_matches=manager.session_lifecycle_snapshot_matches,
        acquire_lifecycle_admission=manager.acquire_lifecycle_admission,
        attachment_capture_status=lambda: asyncio.sleep(0, result="unavailable"),
        attachment_config_generation=lambda: None,
    )
    manager.controller.memory_adapter = adapter
    assert adapter.start(task_factory=asyncio.create_task)
    context = _context()
    context.message_id = "native-memory-turn"
    context.is_original_human_text = True
    context.platform_specific["author_id"] = "authenticated-author"
    adapter.offer(
        memory_turn_event(
            context,
            "记住这一轮",
            "ses_fsm",
            manager.snapshot_session_lifecycle("ses_fsm"),
        )
    )
    await asyncio.wait_for(capture_started.wait(), timeout=1.0)

    delivered = await asyncio.wait_for(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="next turn",
            ),
            context=_context(),
        ),
        timeout=1.0,
    )
    assert delivered.turn_id
    assert starts[-1] == (delivered.turn_id, "next turn")

    async def reset_session() -> str:
        return "reset"

    async def archive_session() -> str:
        return "archived"

    assert await asyncio.wait_for(
        manager.run_session_lifecycle(
            "ses_fsm",
            reset_session,
            deadline_seconds=0.05,
        ),
        timeout=1.0,
    ) == "reset"
    adapter.offer(SessionReset("ses_fsm"))
    assert await asyncio.wait_for(
        manager.run_session_lifecycle(
            "ses_fsm",
            archive_session,
            deadline_seconds=0.05,
        ),
        timeout=1.0,
    ) == "archived"
    adapter.offer(SessionArchived("ses_fsm"))

    await adapter.wait_idle_for_tests(timeout_seconds=1.0)
    assert module.barriers == ["ses_fsm", "ses_fsm"]
    assert module.capacities and all(not item.active for item in module.capacities)
    assert module.reservations and all(
        not item.active for item in module.reservations
    )
    assert adapter.capture_tasks == set()
    await adapter.cancel_memory_capture_tasks()


@pytest.mark.anyio
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


def _configure_activation_owner(
    manager: SessionTurnManager,
    registry: RuntimeActivationRegistry,
    identity,
) -> None:
    manager.controller.runtime_activation = registry
    manager.controller.agent_service.activation_registry = registry
    manager.controller.agent_service.runtime_activation_identity_for_session_binding = (
        lambda _backend, **_binding: RuntimeActivationResolution(
            authoritative=True,
            identity=identity,
        )
    )


def test_hfr_137_opencode_turn_claim_commit_precedes_cleanup(
    managers,
    monkeypatch,
) -> None:
    """HFR-137: the Turn owner commits before shared-server cleanup observes it."""

    manager, _other, engine, engine_b, starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_backend="opencode", agent_variant="opencode")
        )
    registry = RuntimeActivationRegistry()
    identity = registry.attach("opencode", "http://127.0.0.1:4096")
    _configure_activation_owner(manager, registry, identity)
    claim_entered = threading.Event()
    allow_claim = threading.Event()
    retire_started = threading.Event()
    order: list[str] = []
    retired: list[bool] = []
    delivered: list = []
    original_claim = delivery_store.claim_start_batch

    def claim_with_barrier(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        order.append("claim-written")
        claim_entered.set()
        assert allow_claim.wait(timeout=2)
        order.append("claim-returned")
        return result

    monkeypatch.setattr(delivery_store, "claim_start_batch", claim_with_barrier)

    def admit() -> None:
        delivered.append(
            asyncio.run(
                manager.deliver(
                    DeliveryRequest(
                        session_id="ses_fsm",
                        priority="p3",
                        content="OpenCode prompt",
                    ),
                    context=_context(),
                )
            )
        )

    def retire() -> None:
        retire_started.set()

        def final_predicate() -> bool:
            order.append("cleanup-snapshot")
            with engine_b.connect() as conn:
                return delivery_store.active_turn(conn, "ses_fsm") is None

        retired.append(registry.retire_if_current(identity, final_predicate))

    admission_thread = threading.Thread(target=admit)
    retirement_thread = threading.Thread(target=retire)
    admission_thread.start()
    assert claim_entered.wait(timeout=2)
    retirement_thread.start()
    assert retire_started.wait(timeout=2)
    allow_claim.set()
    admission_thread.join(timeout=2)
    retirement_thread.join(timeout=2)

    assert not admission_thread.is_alive()
    assert not retirement_thread.is_alive()
    assert retired == [False]
    assert order == ["claim-written", "claim-returned", "cleanup-snapshot"]
    assert delivered[0].state == "claimed"
    assert starts == [(delivered[0].turn_id, "OpenCode prompt")]


def test_hfr_137_opencode_cleanup_first_preserves_queued_turn_owner(
    managers,
) -> None:
    """HFR-137: a retired shared server cannot consume a queued Turn owner."""

    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_backend="opencode", agent_variant="opencode")
        )
    registry = RuntimeActivationRegistry()
    identity = registry.attach("opencode", "http://127.0.0.1:4096")
    _configure_activation_owner(manager, registry, identity)
    predicate_entered = threading.Event()
    allow_cleanup = threading.Event()
    delivery_started = threading.Event()
    retired: list[bool] = []
    delivered: list = []

    def retire() -> None:
        def final_predicate() -> bool:
            predicate_entered.set()
            assert allow_cleanup.wait(timeout=2)
            return True

        retired.append(registry.retire_if_current(identity, final_predicate))

    def admit() -> None:
        delivery_started.set()
        delivered.append(
            asyncio.run(
                manager.deliver(
                    DeliveryRequest(
                        session_id="ses_fsm",
                        priority="p3",
                        content="queued after cleanup",
                    ),
                    context=_context(),
                )
            )
        )

    retirement_thread = threading.Thread(target=retire)
    admission_thread = threading.Thread(target=admit)
    retirement_thread.start()
    assert predicate_entered.wait(timeout=2)
    admission_thread.start()
    assert delivery_started.wait(timeout=2)
    allow_cleanup.set()
    retirement_thread.join(timeout=2)
    admission_thread.join(timeout=2)

    assert not retirement_thread.is_alive()
    assert not admission_thread.is_alive()
    assert retired == [True]
    assert delivered[0].state == "queued"
    assert delivered[0].turn_id is None
    assert starts == []
    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0]["state"] == "queued"
    assert rows[0]["turn_id"] is None


def test_hfr_137_turn_claim_rechecks_exact_durable_binding(managers) -> None:
    """HFR-137: rebind between observation and commit keeps input queued."""

    manager, _other, engine, engine_b, starts = managers
    registry = RuntimeActivationRegistry()
    identity = registry.attach("codex", "/tmp")
    manager.controller.runtime_activation = registry
    manager.controller.agent_service.activation_registry = registry

    def rebind_before_boundary(_backend, **_binding):
        with engine_b.begin() as conn:
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == "ses_fsm")
                .values(session_anchor="ses_rebound")
            )
        return RuntimeActivationResolution(authoritative=True, identity=identity)

    manager.controller.agent_service.runtime_activation_identity_for_session_binding = (
        rebind_before_boundary
    )

    delivered = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="must follow the rebound Session",
            ),
            context=_context(),
        )
    )

    assert delivered.state == "queued"
    assert delivered.turn_id is None
    assert starts == []
    assert _rows(engine)[0]["state"] == "queued"


def test_hfr_137_cleanup_first_preserves_waiting_replacement_owner(managers) -> None:
    """HFR-137: waiting -> starting also rejects a retired exact generation."""

    manager, _other, engine, _engine_b, _starts = managers
    registry = RuntimeActivationRegistry()
    identity = registry.attach("codex", "/tmp")
    _configure_activation_owner(manager, registry, identity)
    active_turn_id, _context_value = asyncio.run(_activate(manager))
    replacement = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replacement",
            ),
            context=_context(),
        )
    )
    assert replacement.delivery_id
    with engine.begin() as conn:
        active = delivery_store.get_turn(conn, active_turn_id)
        successor_id = str(active["control_successor_turn_id"])
        settled = manager._write_terminal_snapshot(
            conn,
            active_turn_id,
            outcome="completed",
            settled_by="native_terminal",
            evidence_kind="native_terminal",
        )
        assert settled["changed"]
    assert registry.retire_if_current(identity, lambda: True)

    resumed = asyncio.run(
        manager._resume_linked_control_successor("ses_fsm", active_turn_id)
    )

    assert resumed is None
    with engine.connect() as conn:
        successor = delivery_store.get_turn(conn, successor_id)
        delivery = delivery_store.get_delivery(conn, str(replacement.delivery_id))
        predecessor = delivery_store.get_turn(conn, active_turn_id)
    assert successor is not None and successor["state"] == "waiting"
    assert delivery is not None and delivery["state"] == "interrupt_waiting"
    assert predecessor is not None and predecessor["control_state"] != "settled"


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
    assert stored["created_at"] == messages_service.canonical_message_timestamp(
        accepted[0]["submitted_at"]
    )
    assert stored["delivered_at"] == accepted[0]["materialized_at"]


def test_fifo_segment_merges_only_one_authenticated_author(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    active_turn_id, _ = asyncio.run(_activate(manager, text="active"))
    queued = [
        asyncio.run(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content=text,
                    author_id=user_id,
                    message_kind="original",
                ),
                context=_context(),
            )
        )
        for text, user_id in (
            ("alice one", "remote:alice"),
            ("alice two", "remote:alice"),
            ("bob one", "remote:bob"),
        )
    ]

    assert asyncio.run(manager.terminalize_turn(active_turn_id))
    queued_starts = [(turn_id, text) for turn_id, text in starts if turn_id != active_turn_id]
    assert len(queued_starts) == 1
    alice_turn_id, dispatch_text = queued_starts[0]
    assert dispatch_text == "alice one\nalice two"

    first, second, third = [_row(engine, str(item.delivery_id)) for item in queued]
    assert first["turn_id"] == alice_turn_id
    assert second["turn_id"] == alice_turn_id
    assert third["turn_id"] is None
    assert third["state"] == "queued"


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


def test_memory_indep_016_new_queue_uses_core_identity_without_memory_metadata(
    managers,
) -> None:
    """Scenario: MEMORY-INDEP-016."""

    manager, _other, engine, _engine_b, _starts = managers
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="Continue",
                author_id="remote:alice",
                message_kind="quick_reply",
                metadata={
                    "quick_reply_for": "msg-agent",
                    "_memory_user_id": "forged-principal",
                    "_memory_ordinary_text": True,
                    "_memory_cli_admitted": True,
                },
            ),
            context=_context(),
        )
    )

    row = _row(engine, str(result.delivery_id))
    snapshot = json.loads(row["snapshot_json"])
    metadata = json.loads(snapshot["metadata_json"])
    assert snapshot["author_id"] == "remote:alice"
    assert snapshot["message_kind"] == "quick_reply"
    assert not any(key.startswith("_memory_") for key in metadata)


def test_core_message_kind_separates_original_and_quick_reply_segments() -> None:
    common = {
        "scope_id": "scope",
        "platform": "avibe",
        "author": "user",
        "type": "user",
        "source": "user",
        "author_id": "remote:alice",
        "author_name": "Alice",
        "parent_native_message_id": None,
        "metadata": {},
    }
    original = {**common, "message_kind": "original"}
    quick_reply = {**common, "message_kind": "quick_reply"}

    assert _collect_delivery_segment([original, quick_reply]) == [original]


def test_legacy_and_new_original_rows_do_not_merge_admission_schemas() -> None:
    legacy = {
        "scope_id": "scope",
        "platform": "avibe",
        "author": "user",
        "type": "user",
        "source": "user",
        "author_id": "remote:alice",
        "author_name": "Alice",
        "parent_native_message_id": None,
        "metadata": {"_memory_ordinary_text": True},
    }
    current = {
        **legacy,
        "message_kind": "original",
        "metadata": {},
    }

    assert delivery_store.message_merge_identity(legacy) != (
        delivery_store.message_merge_identity(current)
    )


def test_raw_legacy_and_new_original_snapshots_do_not_merge_admission_schemas() -> None:
    common = {
        "scope_id": "scope",
        "platform": "avibe",
        "author": "user",
        "type": "user",
        "source": "user",
        "author_id": "remote:alice",
        "author_name": "Alice",
        "parent_native_message_id": None,
    }
    legacy = {
        **common,
        "metadata_json": json.dumps({"_memory_ordinary_text": True}),
    }
    current = {
        **common,
        "message_kind": "original",
        "metadata_json": "{}",
    }

    assert delivery_store.message_merge_identity(legacy) != (
        delivery_store.message_merge_identity(current)
    )


@pytest.mark.parametrize("metadata_field", delivery_store.LEGACY_MEMORY_MERGE_IDENTITY_METADATA_KEYS)
def test_every_released_memory_admission_fact_fences_delivery_segments(
    metadata_field: str,
) -> None:
    metadata = {
        delivery_store.LEGACY_MEMORY_USER_ID_METADATA: "principal-a",
        delivery_store.LEGACY_MEMORY_ORDINARY_TEXT_METADATA: True,
        delivery_store.LEGACY_MEMORY_CLI_ADMITTED_METADATA: True,
    }
    common = {
        "scope_id": "scope",
        "platform": "avibe",
        "author": "user",
        "type": "user",
        "source": "user",
        "author_id": "same-core-author",
        "author_name": "Alice",
        "parent_native_message_id": None,
    }
    first = {**common, "metadata": metadata}
    changed_metadata = dict(metadata)
    value = changed_metadata[metadata_field]
    changed_metadata[metadata_field] = not value if isinstance(value, bool) else f"{value}-other"
    second = {**common, "metadata": changed_metadata}

    first_identity = delivery_store.message_merge_identity(first)
    second_identity = delivery_store.message_merge_identity(second)

    assert first_identity[-1] == delivery_store.legacy_memory_merge_identity(metadata)
    assert second_identity[-1] == delivery_store.legacy_memory_merge_identity(changed_metadata)
    assert first_identity != second_identity
    assert _collect_delivery_segment([first, second]) == [first]


def test_scheduled_segment_key_keeps_source_sessions_separate() -> None:
    def row(source_session_id: str) -> dict:
        return {
            "metadata": {
                SCHEDULED_PROVENANCE_KEY: {
                    "platform_specific": {
                        "task_trigger_kind": "agent_run",
                        "source_session_id": source_session_id,
                    }
                }
            }
        }

    assert _scheduled_merge_key(row("source-a")) != _scheduled_merge_key(row("source-b"))
    assert _scheduled_merge_key(row("source-a")) == _scheduled_merge_key(row("source-a"))

    agent_row = row("")
    agent_row["metadata"][SCHEDULED_PROVENANCE_KEY]["platform_specific"].update(
        {"source_kind": "agent", "source_actor": "source-agent-a"}
    )
    other_agent_row = row("")
    other_agent_row["metadata"][SCHEDULED_PROVENANCE_KEY]["platform_specific"].update(
        {"source_kind": "agent", "source_actor": "source-agent-b"}
    )
    assert _scheduled_merge_key(agent_row) != _scheduled_merge_key(other_agent_row)


def test_fifo_scheduled_segment_does_not_merge_different_source_sessions(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    active_turn_id, _ = asyncio.run(_activate(manager, text="active"))

    def scheduled_request(text: str, source_session_id: str) -> DeliveryRequest:
        return DeliveryRequest(
            session_id="ses_fsm",
            priority="p3",
            content=text,
            source="harness",
            author="harness",
            message_type="harness",
            metadata={
                SCHEDULED_PROVENANCE_KEY: {
                    "platform_specific": {
                        "task_trigger_kind": "agent_run",
                        "source_session_id": source_session_id,
                    }
                }
            },
        )

    queued = [
        asyncio.run(manager.deliver(scheduled_request(text, source), context=_context()))
        for text, source in (("callback A", "source-a"), ("callback B", "source-b"))
    ]

    assert asyncio.run(manager.terminalize_turn(active_turn_id))
    queued_starts = [(turn_id, text) for turn_id, text in starts if turn_id != active_turn_id]
    assert len(queued_starts) == 1
    first_turn_id, dispatch_text = queued_starts[0]
    assert dispatch_text == "callback A"
    assert _row(engine, str(queued[0].delivery_id))["turn_id"] == first_turn_id
    assert _row(engine, str(queued[1].delivery_id))["turn_id"] is None


@pytest.mark.anyio
async def test_scheduled_submit_decorates_before_native_steering(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    decorator = AsyncMock(side_effect=lambda _context, text, **_kwargs: f"decorated: {text}")
    manager.controller.message_handler = SimpleNamespace(
        _prepend_message_metadata=decorator,
    )
    await _activate(manager, text="active")
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    context = _context()
    context.platform_specific.update(
        {
            "task_trigger_kind": "agent_run",
            "source_session_id": "source-session",
        }
    )
    result = await manager.deliver(
        DeliveryRequest(
            session_id="ses_fsm",
            priority="p1",
            content="callback result",
            source="harness",
            author="harness",
            message_type="harness",
            metadata={
                SCHEDULED_PROVENANCE_KEY: {
                    "platform_specific": {
                        "task_trigger_kind": "agent_run",
                        "source_session_id": "source-session",
                    }
                }
            },
        ),
        context=context,
    )

    assert result.state == "accepted"
    decorator.assert_awaited_once_with(context, "callback result", include_user_info=False)
    manager._steer.assert_awaited_once()
    assert manager._steer.await_args.args[1].text == "decorated: callback result"


def test_persisted_start_attempt_reaches_dispatch_context(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    captured: dict[str, object] = {}

    async def capture_run(_session_id, context, _text, **_kwargs):
        captured.update(context.platform_specific or {})
        _complete_capture_admission(context)

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


@pytest.mark.anyio
async def test_persisted_scheduled_start_preserves_raw_text_for_handler_routing(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    captured: dict[str, object] = {}

    async def capture_run(_session_id, context, text, **_kwargs):
        captured["context"] = context
        captured["text"] = text

    manager._run = capture_run
    context = _context()
    context.platform_specific.update(
        {
            "task_trigger_kind": "agent_run",
            "source_kind": "agent",
            "source_actor": "source-session",
        }
    )

    admitted = await manager.deliver(
        DeliveryRequest(
            session_id="ses_fsm",
            priority="p3",
            content="callback result",
            source="harness",
            author="harness",
            message_type="harness",
            metadata={
                SCHEDULED_PROVENANCE_KEY: {
                    "platform_specific": {
                        "task_trigger_kind": "agent_run",
                        "source_kind": "agent",
                        "source_actor": "source-session",
                    }
                }
            },
        ),
        context=context,
    )

    assert admitted.state == "claimed"
    assert captured["text"] == "callback result"
    assert captured["context"].platform_specific["source_actor"] == "source-session"
    assert not captured["context"].platform_specific.get(
        SCHEDULED_DISPATCH_METADATA_APPLIED_KEY
    )


def test_first_delivery_binds_agentless_session_before_runtime_start(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
    manager.controller.resolve_vibe_agent_for_context = lambda *_args, **_kwargs: SimpleNamespace(
        id="agent-codex",
        name="reviewer",
        backend="codex",
    )

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="bind the default Agent",
            ),
            context=_agentless_context(),
        )
    )

    assert result.state == "claimed"
    assert [text for _turn_id, text in starts] == ["bind the default Agent"]
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
                agent_sessions.c.agent_variant,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == ("agent-codex", "reviewer", "codex", "codex")


def test_first_delivery_keeps_concurrent_session_binding_winner(managers) -> None:
    manager, _other, engine, engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )

    def resolve_after_user_edit(*_args, **_kwargs):
        with engine_b.begin() as conn:
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == "ses_fsm")
                .values(
                    agent_id="agent-claude",
                    agent_name="writer",
                    agent_backend="claude",
                    agent_variant="claude",
                )
            )
        return SimpleNamespace(id="agent-codex", name="reviewer", backend="codex")

    manager.controller.resolve_vibe_agent_for_context = resolve_after_user_edit

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="keep the user's route",
            ),
            context=_agentless_context(),
        )
    )

    assert result.state == "claimed"
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
        turn = delivery_store.get_turn(conn, str(result.turn_id))
    assert binding == ("agent-claude", "writer", "claude")
    assert turn is not None and turn["backend"] == "claude"


def test_first_delivery_honors_context_agent_override_when_binding(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
    context = _agentless_context()
    context.platform_specific.update(
        {"vibe_agent_id": "agent-selected", "vibe_agent_name": "selected"}
    )
    resolved_kwargs: dict[str, object] = {}

    def resolve(_context, **kwargs):
        resolved_kwargs.update(kwargs)
        return SimpleNamespace(
            id="agent-selected",
            name="selected",
            backend="claude",
        )

    manager.controller.resolve_vibe_agent_for_context = resolve

    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="use selected"),
            context=context,
        )
    )

    assert result.state == "claimed"
    assert resolved_kwargs["override_agent_id"] == "agent-selected"
    assert resolved_kwargs["override_agent_name"] == "selected"
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == ("agent-selected", "selected", "claude")


def test_first_delivery_rejects_unresolvable_context_agent_override(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
    context = _agentless_context()
    context.platform_specific["vibe_agent_name"] = "archived-agent"
    resolved_kwargs: dict[str, object] = {}

    def resolve(_context, **kwargs):
        resolved_kwargs.update(kwargs)
        return None

    manager.controller.resolve_vibe_agent_for_context = resolve

    with pytest.raises(RuntimeError, match="Explicit Vibe Agent override"):
        asyncio.run(
            manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p3", content="reject"),
                context=context,
            )
        )
    assert resolved_kwargs["required"] is True
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == (None, "")


def test_binding_refreshes_all_cached_route_projections(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    context = _agentless_context()
    context.platform_specific.update(
        {
            "vibe_agent_id": "stale-agent",
            "vibe_agent_name": "stale",
            "agent_run_target": {
                "agent_session_id": "ses_fsm",
                "agent_id": "stale-agent",
                "agent_name": "stale",
                "agent_backend": "claude",
                "agent_variant": "claude",
                "model": "old-model",
                "reasoning_effort": "low",
                "workdir": "/tmp",
            },
            "resolved_vibe_agent": {
                "id": "stale-agent",
                "name": "stale",
                "backend": "claude",
            },
        }
    )

    manager._apply_session_binding_to_context(
        context,
        {
            "id": "ses_fsm",
            "agent_id": None,
            "agent_name": None,
            "agent_backend": "codex",
            "agent_variant": "codex",
            "model": None,
            "reasoning_effort": None,
            "metadata_json": '{"explicit_setting_overrides":["model"]}',
        },
    )

    spec = context.platform_specific
    assert spec["vibe_agent_id"] is None
    assert spec["vibe_agent_name"] is None
    assert spec["agent_session_target"]["agent_backend"] == "codex"
    assert spec["agent_run_target"]["agent_backend"] == "codex"
    assert spec["agent_run_target"]["agent_name"] is None
    assert spec["agent_run_target"]["model"] is None
    assert spec["agent_session_target"]["metadata"] == {"explicit_setting_overrides": ["model"]}
    assert "resolved_vibe_agent" not in spec


def test_name_only_scheduled_projection_refreshes_same_durable_agent(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    context = _context()
    context.platform_specific.update(
        {
            "turn_source": SOURCE_SCHEDULED,
            "vibe_agent_name": "reviewer",
            "agent_session_target": {
                "id": "ses_fsm",
                "agent_id": "agent-reviewer",
                "agent_name": "reviewer",
                "agent_backend": "claude",
                "agent_variant": "claude",
                "model": "old-model",
                "reasoning_effort": "low",
                "metadata": {},
            },
        }
    )

    assert manager._binding_projection_is_stale(
        context,
        {
            "id": "ses_fsm",
            "agent_id": "agent-reviewer",
            "agent_name": "reviewer",
            "agent_backend": "codex",
            "agent_variant": "codex",
            "model": "new-model",
            "reasoning_effort": "high",
            "metadata_json": '{"explicit_setting_overrides":["model"]}',
        },
    )


def test_projection_refresh_preserves_explicit_run_target(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    context = _context()
    context.platform_specific.update(
        {
            "agent_run_target": {
                "agent_session_id": "ses_scheduled_run",
                "agent_id": "agent-writer",
                "agent_name": "writer",
                "agent_backend": "claude",
            },
            "resolved_vibe_agent": {
                "id": "agent-writer",
                "name": "writer",
                "backend": "claude",
            },
        }
    )

    assert not manager._binding_projection_is_stale(
        context,
        {
            "id": "ses_fsm",
            "agent_id": "agent-reviewer",
            "agent_name": "reviewer",
            "agent_backend": "codex",
            "metadata_json": "{}",
        },
    )


def test_fifo_head_mismatch_does_not_bind_session_route(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
        delivery_store.insert_delivery(
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
                source="scheduled",
                text="exact head",
                metadata={
                    SCHEDULED_PROVENANCE_KEY: {
                        "platform_specific": {
                            "vibe_agent_name": "reviewer",
                        }
                    }
                },
            ),
            dispatch_text="exact head",
            now="2026-08-01T00:00:01Z",
        )
        head = delivery_store.get_delivery(conn, delivery_id)
    assert head is not None

    assert not asyncio.run(
        manager.drain_delivery_queue(
            "ses_fsm",
            expected_head_id="different-head",
            expected_head_version=int(head["version"]),
        )
    )
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == (None, None, "")
    assert starts == []


def test_im_p1_materializes_only_after_exact_native_acceptance(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, str]:
        active_turn_id, _ = await _activate(manager, text="active")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def accept(_backend, request):
            assert request.text == "steer this exact text"
            entered.set()
            await release.wait()
            return steer_result(SteerOutcome.ACCEPTED)

        manager._steer = accept
        pending = asyncio.create_task(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p1",
                    content="steer this exact text",
                    platform="slack",
                    source="user",
                    author="user",
                    message_type="user",
                    author_id="U42",
                    author_name="Ada",
                    native_message_id="slack-msg-42",
                ),
                context=_context(),
            )
        )
        await entered.wait()
        with engine.connect() as conn:
            delivery = conn.execute(
                select(message_deliveries).where(
                    message_deliveries.c.dedupe_key == "slack:slack-msg-42"
                )
            ).mappings().one()
            materialized = conn.execute(
                select(messages.c.id).where(
                    messages.c.native_message_id == "slack-msg-42"
                )
            ).first()
        assert delivery["state"] == "steering"
        assert delivery["message_id"] is None
        assert materialized is None

        release.set()
        accepted = await pending
        assert accepted.state == "accepted"
        return active_turn_id, str(accepted.delivery_id)

    turn_id, delivery_id = asyncio.run(run())
    with engine.connect() as conn:
        delivery = delivery_store.get_delivery(conn, delivery_id)
        message = conn.execute(
            select(messages).where(messages.c.id == delivery_id)
        ).mappings().one()
    assert delivery is not None
    assert delivery["turn_id"] == turn_id
    assert delivery["message_id"] == delivery_id
    assert message["platform"] == "slack"
    assert message["native_message_id"] == "slack-msg-42"
    assert message["content_text"] == "steer this exact text"
    assert message["author_id"] == "U42"


def _record_admission_acks(manager: SessionTurnManager) -> list[tuple[str, str, str, str]]:
    """Capture every admission receipt as (platform, reaction target, state, admission)."""

    acks: list[tuple[str, str, str, str]] = []

    async def record_ack(context, *, state, admission=""):
        spec = context.platform_specific or {}
        target = spec.get("processing_indicator_message_id") or context.message_id
        acks.append((str(context.platform), str(target), state, admission))
        return None

    manager.controller.processing_indicator = SimpleNamespace(
        ack_delivery_state=record_ack
    )
    return acks


def test_late_steer_acceptance_upgrades_the_queued_admission_receipt(managers) -> None:
    """MESSAGE-DELIVERY-303.

    An input admitted as ``pending_steer`` already told its sender it was queued.
    The attempt that actually accepts it runs in ``_run_pending_steers``, whose
    result no ingress caller ever sees, so this loop is the only place that can
    correct the receipt.
    """

    manager, _other, engine, _engine_b, _starts = managers
    acks = _record_admission_acks(manager)

    async def run() -> None:
        turn_id, _ = await _activate(manager, text="active")
        delivery_id = delivery_store.new_delivery_id()
        with engine.begin() as conn:
            row = delivery_store.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id="ses_fsm",
                priority="p1",
                state="reserved",
                snapshot=delivery_store.message_snapshot(
                    scope_id=None,
                    session_id="ses_fsm",
                    platform="slack",
                    author="user",
                    source="user",
                    message_type="user",
                    text="late steer",
                    native_message_id="slack-msg-99",
                ),
                dispatch_text="late steer",
            )
            pending = delivery_store.open_pending_steer_batch(
                conn,
                deliveries=[row],
                turn_id=turn_id,
                attempt_id=delivery_store.new_attempt_id(),
            )
            assert len(pending) == 1
            assert pending[0]["state"] == "pending_steer"

        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
        await manager._run_pending_steers("ses_fsm", turn_id, _context())

        with engine.connect() as conn:
            settled = delivery_store.get_delivery(conn, delivery_id)
        assert settled is not None
        assert settled["state"] == "accepted"

    asyncio.run(run())

    assert acks == [("slack", "slack-msg-99", "accepted", "steered")]


@pytest.mark.anyio
async def test_pending_scheduled_batches_each_get_dispatch_metadata(managers) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    decorator = AsyncMock(side_effect=lambda _context, text, **_kwargs: f"decorated: {text}")
    manager.controller.message_handler = SimpleNamespace(
        _prepend_message_metadata=decorator,
    )
    context = _context()
    deliveries = []
    for index, text in enumerate(("first callback", "second callback")):
        deliveries.append(
            {
                "id": f"delivery-{index}",
                "dispatch_text": text,
                "snapshot_json": json.dumps(
                    delivery_store.message_snapshot(
                        scope_id=None,
                        session_id="ses_fsm",
                        platform="avibe",
                        author="harness",
                        source="harness",
                        message_type="harness",
                        text=text,
                        metadata={
                            SCHEDULED_PROVENANCE_KEY: {
                                "platform_specific": {
                                    "task_trigger_kind": "agent_run",
                                    "source_session_id": f"source-{index}",
                                }
                            }
                        },
                    )
                ),
            }
        )

    assert await manager.prepare_scheduled_dispatch(
        context, "first callback", delivery=deliveries[0]
    ) == "decorated: first callback"
    assert await manager.prepare_scheduled_dispatch(
        context, "second callback", delivery=deliveries[1]
    ) == "decorated: second callback"
    assert decorator.await_count == 2
    assert [call.args[1] for call in decorator.await_args_list] == [
        "first callback",
        "second callback",
    ]


def test_workbench_delivery_reports_no_reaction_receipt(managers) -> None:
    """Only an IM input has a native message to react on."""

    manager, _other, engine, _engine_b, _starts = managers
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        row = delivery_store.insert_delivery(
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
                message_type="user",
                text="composer input",
            ),
            dispatch_text="composer input",
        )

    assert manager._delivery_receipt_context("ses_fsm", row) is None


def test_quick_reply_receipt_uses_its_persisted_echo_target(managers) -> None:
    """MESSAGE-DELIVERY-306.

    A quick-reply callback is dispatched with ``message_id=None`` on purpose, so
    its Delivery snapshot carries no native message id and the persisted echo id
    is the only reaction target it will ever have.
    """

    manager, _other, engine, _engine_b, _starts = managers
    acks = _record_admission_acks(manager)

    async def run() -> None:
        turn_id, _ = await _activate(manager, text="active")
        delivery_id = delivery_store.new_delivery_id()
        with engine.begin() as conn:
            row = delivery_store.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id="ses_fsm",
                priority="p1",
                state="reserved",
                snapshot=delivery_store.message_snapshot(
                    scope_id=None,
                    session_id="ses_fsm",
                    platform="telegram",
                    author="user",
                    source="user",
                    message_type="user",
                    text="quick reply",
                ),
                dispatch_text="quick reply",
                history_event={
                    "kind": "admission",
                    "context": {"processing_indicator_message_id": "echo-7"},
                },
            )
            delivery_store.open_pending_steer_batch(
                conn,
                deliveries=[row],
                turn_id=turn_id,
                attempt_id=delivery_store.new_attempt_id(),
            )

        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
        await manager._run_pending_steers("ses_fsm", turn_id, _context())

    asyncio.run(run())

    assert acks == [("telegram", "echo-7", "accepted", "steered")]


def test_definitive_steer_refusal_after_archive_reports_the_retired_receipt(
    managers,
) -> None:
    """MESSAGE-DELIVERY-307.

    A Delivery refused after its Session went inactive is retired, never starts a
    Turn of its own, and would otherwise keep the 👌 that promised it would run.
    """

    manager, _other, engine, _engine_b, _starts = managers
    acks = _record_admission_acks(manager)

    async def run() -> None:
        turn_id, _ = await _activate(manager, text="active")
        delivery_id = delivery_store.new_delivery_id()
        with engine.begin() as conn:
            row = delivery_store.insert_delivery(
                conn,
                delivery_id=delivery_id,
                session_id="ses_fsm",
                priority="p1",
                state="reserved",
                snapshot=delivery_store.message_snapshot(
                    scope_id=None,
                    session_id="ses_fsm",
                    platform="slack",
                    author="user",
                    source="user",
                    message_type="user",
                    text="refused steer",
                    native_message_id="slack-msg-77",
                ),
                dispatch_text="refused steer",
            )
            delivery_store.open_pending_steer_batch(
                conn,
                deliveries=[row],
                turn_id=turn_id,
                attempt_id=delivery_store.new_attempt_id(),
            )
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == "ses_fsm")
                .values(status="archived")
            )

        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.REFUSED))
        await manager._run_pending_steers("ses_fsm", turn_id, _context())

        with engine.connect() as conn:
            settled = delivery_store.get_delivery(conn, delivery_id)
        assert settled is not None
        assert settled["state"] == "retired"

    asyncio.run(run())

    assert acks == [("slack", "slack-msg-77", "retired", "")]


def test_merged_batch_exposes_every_reaction_target(managers) -> None:
    """MESSAGE-DELIVERY-309.

    Only the first Delivery of a merged Turn hydrates the dispatch context, so
    the Turn has to carry the reaction targets of the others for the processing
    indicator to clear their receipts.
    """

    manager, _other, engine, _engine_b, _starts = managers
    rows = []
    with engine.begin() as conn:
        for index, echo in enumerate(("echo-1", "echo-2")):
            rows.append(
                delivery_store.insert_delivery(
                    conn,
                    delivery_id=delivery_store.new_delivery_id(),
                    session_id="ses_fsm",
                    priority="p3",
                    state="queued",
                    snapshot=delivery_store.message_snapshot(
                        scope_id=None,
                        session_id="ses_fsm",
                        platform="telegram",
                        author="user",
                        source="user",
                        message_type="user",
                        text=f"quick reply {index}",
                    ),
                    dispatch_text=f"quick reply {index}",
                    history_event={
                        "kind": "admission",
                        "context": {"processing_indicator_message_id": echo},
                    },
                )
            )

    context = _context()
    manager._hydrate_delivery_batch_context(context, rows)

    assert context.platform_specific["delivery_ack_targets"] == ["echo-1", "echo-2"]


def test_recovered_reservation_reports_its_admission_receipt(managers) -> None:
    """MESSAGE-DELIVERY-308.

    A reservation committed before the service stopped is re-entered by recovery,
    not by the ingress handler, so recovery is the only reporter of its outcome.
    """

    manager, _other, engine, _engine_b, _starts = managers
    acks = _record_admission_acks(manager)

    async def run() -> None:
        await _activate(manager, text="active")
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
                    platform="slack",
                    author="user",
                    source="user",
                    message_type="user",
                    text="recovered input",
                    native_message_id="slack-msg-55",
                ),
                dispatch_text="recovered input",
            )

        await manager.recover_durable_delivery_state("ses_fsm")

        with engine.connect() as conn:
            settled = delivery_store.get_delivery(conn, delivery_id)
        assert settled is not None
        assert settled["state"] == "queued"

    asyncio.run(run())

    assert acks == [("slack", "slack-msg-55", "queued", "")]


def test_duplicate_im_p1_reuses_one_delivery_and_one_native_steer(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, str]:
        await _activate(manager, text="active")
        manager._steer = AsyncMock(
            return_value=steer_result(SteerOutcome.ACCEPTED)
        )
        request = DeliveryRequest(
            session_id="ses_fsm",
            priority="p1",
            content="deliver once",
            platform="slack",
            source="user",
            author="user",
            message_type="user",
            native_message_id="slack-duplicate-1",
        )

        first = await manager.deliver(request, context=_context())
        duplicate = await manager.deliver(request, context=_context())

        manager._steer.assert_awaited_once()
        return first.delivery_id, duplicate.delivery_id

    first_id, duplicate_id = asyncio.run(run())
    assert duplicate_id == first_id
    with engine.connect() as conn:
        rows = conn.execute(
            select(message_deliveries).where(
                message_deliveries.c.dedupe_key == "slack:slack-duplicate-1"
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "accepted"


def test_delivery_admission_context_restores_route_without_message_metadata(
    managers,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="route me",
                metadata={"visible": "record metadata"},
                admission_context={
                    "message_handler_route": {
                        "base_session_id": "slack_C1:reviewer",
                        "subagent_name": "reviewer",
                        "routing_subagent": True,
                    }
                },
            ),
            context=_context(),
        )
    )
    delivery = _row(engine, str(admitted.delivery_id))
    context = _context()

    payload = manager._hydrate_delivery_context(context, delivery)

    assert payload["metadata"] == {"visible": "record metadata"}
    assert context.user_id == "user"
    assert context.platform_specific["delivery_admission_context"] == {
        "message_handler_route": {
            "base_session_id": "slack_C1:reviewer",
            "subagent_name": "reviewer",
            "routing_subagent": True,
        }
    }


def test_hydrate_delivery_context_preserves_im_author_as_routing_identity(
    managers,
) -> None:
    """Hydrating an IM delivery keeps the outbound recipient on author_id.

    This is the guardrail that would have caught avibe-bot/avibe#1584: Memory
    identity must not replace MessageContext.user_id.
    """

    manager, _other, engine, _engine_b, _starts = managers
    author_id = "wxid_real_user"
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="hello from wechat",
                platform="wechat",
                source="user",
                author="user",
                message_type="user",
                author_id=author_id,
                author_name="Ada",
                native_message_id="wc-msg-1",
                metadata={"_memory_user_id": "local"},
            ),
            context=_context(),
        )
    )
    delivery = _row(engine, str(admitted.delivery_id))
    context = MessageContext(
        user_id="workbench",
        channel_id=author_id,
        platform="wechat",
    )

    manager._hydrate_delivery_context(context, delivery)

    assert context.user_id == author_id
    assert context.platform_specific["message_metadata"] == {}


def test_wechat_outbound_send_uses_hydrated_author_id(managers) -> None:
    """Scenario: MESSAGE-DELIVERY-316.

    WeChat reply addresses the real platform user, never a Memory principal.
    """

    from modules.im.wechat import WeChatBot, WeChatConfig

    manager, _other, engine, _engine_b, _starts = managers
    author_id = "wxid_real_user"
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="hello from wechat",
                platform="wechat",
                source="user",
                author="user",
                message_type="user",
                author_id=author_id,
                native_message_id="wc-msg-2",
            ),
            context=_context(),
        )
    )
    delivery = _row(engine, str(admitted.delivery_id))
    context = MessageContext(
        user_id=None,
        channel_id=author_id,
        platform="wechat",
        platform_specific={"context_token": "ctx-1"},
    )
    manager._hydrate_delivery_context(context, delivery)
    bot = WeChatBot(
        WeChatConfig(bot_token="token", base_url="https://ilinkai.weixin.qq.com")
    )

    with patch(
        "modules.im.wechat.wechat_api.send_message",
        new=AsyncMock(return_value={}),
    ) as mock_send:
        message_id = asyncio.run(bot.send_message(context, "reply"))

    to_user_id = mock_send.await_args.args[2]
    assert to_user_id == author_id
    assert to_user_id
    assert message_id


def test_workbench_memory_principal_uses_authenticated_author_id(managers) -> None:
    """Workbench Memory never trusts released `_memory_user_id` metadata."""

    manager, _other, engine, _engine_b, starts = managers
    controller = _memory_facts_controller()
    workbench = _context()
    workbench.user_id = "workbench"
    skipped = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="no memory identity",
            ),
            context=workbench,
        )
    )
    skipped_delivery = _row(engine, str(skipped.delivery_id))
    skipped_context = _context()
    skipped_context.user_id = "workbench"
    manager._hydrate_delivery_context(skipped_context, skipped_delivery)
    skipped_facts = controller._memory_turn_facts(skipped_context)

    assert skipped_context.user_id == "workbench"
    assert skipped_facts.user_id is None
    assert controller.memory_capture_admitted(skipped_context) is False
    assert controller.memory_principal_for_context(skipped_context) is None
    assert starts

    remembered_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=remembered_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="remember this",
                author_id="push-user",
                message_kind="original",
                metadata={
                    "_memory_user_id": "local",
                    "_memory_ordinary_text": True,
                },
            ),
            dispatch_text="remember this",
        )
    context = _context()
    context.user_id = "workbench"
    manager._hydrate_delivery_context(context, _row(engine, remembered_id))
    facts = controller._memory_turn_facts(context)

    assert context.user_id == "push-user"
    assert facts.user_id == "push-user"
    assert controller.memory_principal_for_context(context) == "principal:avibe:push-user"


def test_legacy_workbench_lan_author_does_not_gain_memory_admission(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    snapshot = delivery_store.message_snapshot(
        scope_id=None,
        session_id="ses_fsm",
        platform="avibe",
        author="user",
        source="user",
        text="legacy LAN input",
        author_id="local",
        metadata={
            "_memory_cli_admitted": False,
            "_memory_ordinary_text": True,
        },
    )
    snapshot.pop("message_kind")
    snapshot["metadata_json"] = json.dumps(
        {
            "_memory_cli_admitted": False,
            "_memory_ordinary_text": True,
        }
    )
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=snapshot,
            dispatch_text="legacy LAN input",
        )

    context = _context()
    manager._hydrate_delivery_context(context, _row(engine, delivery_id))
    controller = _memory_facts_controller()

    assert context.user_id == "user"
    assert (context.platform_specific or {}).get("memory_cli_admitted") is None
    assert controller.memory_capture_admitted(context) is False
    assert controller.memory_principal_for_context(context) is None


def test_legacy_workbench_strict_author_keeps_memory_admission(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    snapshot = delivery_store.message_snapshot(
        scope_id=None,
        session_id="ses_fsm",
        platform="avibe",
        author="user",
        source="user",
        text="legacy loopback input",
        author_id="local",
        metadata={
            "_memory_user_id": "local",
            "_memory_cli_admitted": True,
            "_memory_ordinary_text": True,
        },
    )
    snapshot.pop("message_kind")
    snapshot["metadata_json"] = json.dumps(
        {
            "_memory_user_id": "local",
            "_memory_cli_admitted": True,
            "_memory_ordinary_text": True,
        }
    )
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=delivery_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=snapshot,
            dispatch_text="legacy loopback input",
        )

    context = _context()
    manager._hydrate_delivery_context(context, _row(engine, delivery_id))
    controller = _memory_facts_controller()

    assert context.user_id == "local"
    assert (context.platform_specific or {}).get("memory_cli_admitted") is True
    assert controller.memory_capture_admitted(context) is True
    assert controller.memory_principal_for_context(context) == "principal:avibe:local"


@pytest.mark.parametrize("launch_path", ["immediate", "fifo", "recovery"])
def test_durable_workbench_turn_restores_memory_admission_facts(
    managers,
    launch_path: str,
) -> None:
    from core.controller import Controller
    from core.memory_cli_access import configure_memory_cli_access

    manager, _other, engine, _engine_b, _starts = managers
    manager.controller.config.memory = SimpleNamespace(enabled=True)
    classifications: list[bool | None] = []
    routing_users: list[str | None] = []
    memory_users: list[str | None] = []
    memory_cli_observations: list[tuple[bool, bool, tuple[str, str] | None]] = []
    principal_id = "u-" + ("1" * 32)
    project_id = "p-" + ("2" * 32)
    admission = SimpleNamespace(
        principal_for=lambda _facts: principal_id,
        project_for=lambda _facts: project_id,
        admits=lambda _facts: True,
    )
    facts_controller = _memory_facts_controller()
    prompt_controller = SimpleNamespace(
        config=SimpleNamespace(platform="avibe", memory=SimpleNamespace(enabled=True)),
        _memory_scopes_by_session={},
        _memory_cli_facts_by_session={},
        _memory_turn_facts=lambda _context: object(),
        _memory_admission=lambda: admission,
    )
    prompt_controller.configure_memory_cli_session = (
        Controller.configure_memory_cli_session.__get__(prompt_controller)
    )
    prompt_controller.memory_scope_for_cli_session = (
        Controller.memory_scope_for_cli_session.__get__(prompt_controller)
    )

    async def capture_start(_session_id, context, _text, **_kwargs):
        classifications.append(context.is_original_human_text)
        routing_users.append(context.user_id)
        memory_users.append(
            facts_controller._memory_turn_facts(context).user_id
        )
        cli_admitted = configure_memory_cli_access(prompt_controller, context)
        payload = context.platform_specific or {}
        memory_cli_observations.append(
            (
                payload.get("memory_cli_admitted") is True,
                cli_admitted,
                prompt_controller.memory_scope_for_cli_session("ses_fsm"),
            )
        )
        _complete_capture_admission(context)

    manager._run = capture_start
    if launch_path == "immediate":
        asyncio.run(
            manager.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content="remember this",
                    author_id="local",
                    message_kind="original",
                    metadata={
                        "_memory_user_id": "forged",
                        "_memory_cli_admitted": True,
                        "_memory_ordinary_text": True,
                    },
                ),
                context=_context(),
            )
        )
    else:
        delivery_id = delivery_store.new_delivery_id()
        with engine.begin() as conn:
            delivery_store.insert_delivery(
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
                        text="remember this",
                        author_id="local",
                        message_kind="original",
                        metadata={
                            "_memory_user_id": "forged",
                        "_memory_cli_admitted": True,
                        "_memory_ordinary_text": True,
                    },
                ),
                dispatch_text="remember this",
            )
        if launch_path == "fifo":
            asyncio.run(manager.drain_delivery_queue("ses_fsm"))
        else:
            asyncio.run(manager.recover_durable_delivery_state(service_restart=True))

    assert classifications == [True]
    assert routing_users == ["local"]
    assert memory_users == ["local"]
    assert memory_cli_observations == [
        (True, True, (principal_id, project_id)),
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"_memory_cli_admitted": False},
        {"_memory_cli_admitted": "true"},
        {"_memory_cli_admitted": 1},
    ],
)
def test_durable_memory_cli_admission_fails_closed(
    managers,
    metadata: dict[str, object],
) -> None:
    manager, _other, _engine, _engine_b, _starts = managers
    admissions: list[object] = []

    def stale_context(_session_id: str) -> MessageContext:
        context = _context()
        context.platform_specific["memory_cli_admitted"] = True
        return context

    async def capture_start(_session_id, context, _text, **_kwargs):
        admissions.append((context.platform_specific or {}).get("memory_cli_admitted"))
        _complete_capture_admission(context)

    manager.bind_context(stale_context)
    manager._run = capture_start
    asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="do not admit",
                metadata=metadata,
            ),
            context=_context(),
        )
    )

    assert admissions == [None]


@pytest.mark.parametrize(
    "message_kind",
    [
        "quick_reply",
        "forwarded",
        "edited",
        "system",
        "unknown",
    ],
)
def test_non_original_message_kinds_do_not_merge_or_expand_admission(
    managers,
    message_kind: str,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    common = {
        "platform": "avibe",
        "source": "user",
        "author": "user",
        "author_id": "remote:alice",
    }
    rows = [
        {**common, "message_kind": "original"},
        {**common, "message_kind": message_kind},
    ]
    assert _collect_delivery_segment(rows) == [rows[0]]

    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
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
                text="not original",
                author_id="remote:alice",
                message_kind=message_kind,
            ),
            dispatch_text="not original",
        )
    context = _context()
    manager._hydrate_delivery_context(context, _row(engine, delivery_id))
    assert context.message_kind == message_kind
    assert context.is_original_human_text is False


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
        _complete_capture_admission(context)

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
            select(agent_sessions.c.agent_status).where(agent_sessions.c.id == "ses_fsm")
        ).scalar_one()
    assert turn is not None and turn["state"] == "active"
    assert status == "running"


def test_terminal_output_persistence_failure_does_not_emit_empty_fallback(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, context = asyncio.run(_activate(manager, text="visible native result"))
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_status="running")
        )
        initial = delivery_store.initial_deliveries_for_turn(conn, turn_id)[0]

    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)

    async def terminal_persistence_failure(_controller, dispatch_context, *_args, **_kwargs):
        set_dispatch_phase(dispatch_context, DISPATCH_PHASE_ATTEMPTING)
        manager.on_terminal_result(
            dispatch_context,
            is_error=False,
            terminal_evidence={"kind": "native_result"},
        )
        raise OSError("terminal output persistence failed")

    async def empty_fallback(_context, _message_type, _text, **_kwargs):
        manager.on_terminal_result(_context, is_error=True)
        manager.on_terminal_delivery_complete(_context)

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        terminal_persistence_failure,
    )
    manager.controller.emit_agent_message = AsyncMock(side_effect=empty_fallback)

    async def run() -> None:
        await manager._run(
            "ses_fsm",
            context,
            "visible native result",
            logical_turn_id=turn_id,
            delivery_id=str(initial["id"]),
            durable_preallocated=True,
        )
        await manager.in_flight["ses_fsm"].task

    asyncio.run(run())

    manager.controller.emit_agent_message.assert_not_awaited()
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


def test_idle_admission_still_respects_an_unresolved_reservation_fence(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    with engine.begin() as conn:
        for delivery_id, state, submitted_at in (
            ("msg_older_backlog", "queued", "2026-08-01T00:00:01Z"),
            ("msg_reserved_fence", "reserved", "2026-08-01T00:00:02Z"),
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
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="new idle admission",
            ),
            context=_context(),
        )
    )

    assert admitted.state == "queued"
    assert [text for _turn, text in starts] == ["msg_older_backlog"]
    with engine.connect() as conn:
        active = delivery_store.active_turn(conn, "ses_fsm")
    assert active is not None
    assert active["initial_delivery_id"] == "msg_older_backlog"
    assert _row(engine, "msg_reserved_fence")["state"] == "reserved"


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


def test_steer_acceptance_materializes_message_before_show_event_link(managers) -> None:
    """MESSAGE-DELIVERY-310: acceptance satisfies the Show event Message FK."""

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
                source="show_annotation",
                text="accepted annotation",
            ),
            dispatch_text="accepted annotation",
        )
        conn.execute(
            show_session_events.insert().values(
                id="show_evt_steer_acceptance",
                session_id="ses_fsm",
                event_type="human.annotation.created",
                actor="human",
                scope="page",
                anchor_json="{}",
                payload_json="{}",
                transcript_text="accepted annotation",
                message_id=None,
                delivery_id=delivery_id,
                created_at="2026-08-07T00:00:00+00:00",
            )
        )

    manager._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")

    async def accepted(_backend, _request):
        return steer_result(SteerOutcome.ACCEPTED)

    manager._steer = accepted
    result = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="ignored retry payload",
                delivery_id=delivery_id,
            ),
            context=_context(),
        )
    )

    assert result.state == "accepted"
    assert result.turn_id == turn_id
    with engine.connect() as conn:
        assert conn.execute(
            select(show_session_events.c.message_id).where(
                show_session_events.c.id == "show_evt_steer_acceptance"
            )
        ).scalar_one() == delivery_id
        assert conn.execute(
            select(messages.c.id).where(messages.c.id == delivery_id)
        ).scalar_one() == delivery_id


def test_lost_accepted_receipt_materializes_from_exact_restart_evidence(
    managers,
    monkeypatch,
) -> None:
    """MESSAGE-DELIVERY-011: accepted evidence recovers without adapter reconciliation."""

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
    persisted = _row(engine, str(outcome.delivery_id))
    assert persisted["state"] == "reconciling_steer"
    assert persisted["current_receipt_outcome"] == "accepted"
    restarted._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    restarted._reconcile_steer_attempt = AsyncMock(
        side_effect=AssertionError("accepted evidence must not reconcile")
    )
    asyncio.run(restarted.recover_durable_delivery_state())

    row = _row(engine, str(outcome.delivery_id))
    assert calls == 1
    restarted._reconcile_steer_attempt.assert_not_awaited()
    assert row["state"] == "accepted"
    assert row["turn_id"] == turn_id
    assert row["current_attempt_id"] is None
    with engine.connect() as conn:
        message = conn.execute(
            select(messages).where(messages.c.id == row["message_id"])
        ).mappings().one()
    assert message["content_text"] == "accepted once"


def test_observation_recovery_materializes_persisted_accepted_receipt_without_reconcile(
    managers,
    monkeypatch,
) -> None:
    """MESSAGE-DELIVERY-311: observation recovery consumes accepted evidence directly."""

    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    first._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    first._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    original = delivery_store.materialize_steer_acceptance

    def lose_receipt(*_args, **_kwargs):
        raise OSError("simulated receipt fsync loss")

    monkeypatch.setattr(
        delivery_store,
        "materialize_steer_acceptance",
        lose_receipt,
    )
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="observed"),
            context=_context(),
        )
    )
    monkeypatch.setattr(delivery_store, "materialize_steer_acceptance", original)
    observations, _ = restarted.scan_runtime_delivery_recovery(
        limit=10,
        occupied=frozenset(),
    )
    observation = next(
        item for item in observations if item.delivery_id == str(outcome.delivery_id)
    )
    restarted._reconcile_steer_attempt = AsyncMock(
        side_effect=AssertionError("accepted evidence must not reconcile")
    )

    assert asyncio.run(restarted.recover_runtime_delivery_observation(observation))
    restarted._reconcile_steer_attempt.assert_not_awaited()
    row = _row(engine, str(outcome.delivery_id))
    assert row["state"] == "accepted"
    assert row["turn_id"] == turn_id


def test_accepted_receipt_recovery_preserves_attempt_batch_and_run_attachment(
    managers,
    monkeypatch,
) -> None:
    """MESSAGE-DELIVERY-313: accepted batch recovery retains Turn participants."""

    first, restarted, engine, _engine_b, _starts = managers
    original = delivery_store.materialize_steer_acceptance
    run_id = "run-accepted-receipt-batch"

    async def run() -> tuple[str, list[str], MessageContext]:
        turn_id, active_context = await _activate(first)
        queued = [
            await first.deliver(
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p3",
                    content=text,
                    source="harness",
                    author="harness",
                    message_type="harness",
                ),
                context=_context(),
            )
            for text in ("batch one", "batch two")
        ]
        now = "2026-08-11T00:00:00Z"
        with engine.begin() as conn:
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    definition_id=None,
                    run_type="agent_run",
                    status="running",
                    cancel_requested=0,
                    session_id="ses_fsm",
                    delivery_id=queued[1].delivery_id,
                    created_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
            )
        first._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

        def lose_receipt(*_args, **_kwargs):
            raise OSError("simulated receipt fsync loss")

        monkeypatch.setattr(
            delivery_store,
            "materialize_steer_acceptance",
            lose_receipt,
        )
        await first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content=None),
            context=_context(),
        )
        monkeypatch.setattr(
            delivery_store,
            "materialize_steer_acceptance",
            original,
        )
        delivery_ids = [str(item.delivery_id) for item in queued]
        persisted = [_row(engine, delivery_id) for delivery_id in delivery_ids]
        assert {row["state"] for row in persisted} == {"reconciling_steer"}
        assert {row["current_receipt_outcome"] for row in persisted} == {
            "accepted"
        }
        assert len({row["current_attempt_id"] for row in persisted}) == 1

        holder = asyncio.create_task(asyncio.Event().wait())
        restarted.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=active_context,
            logical_turn_id=turn_id,
        )
        restarted._reconcile_steer_attempt = AsyncMock(
            side_effect=AssertionError("accepted evidence must not reconcile")
        )
        await restarted.recover_durable_delivery_state()
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        return turn_id, delivery_ids, active_context

    turn_id, delivery_ids, active_context = asyncio.run(run())
    restarted._reconcile_steer_attempt.assert_not_awaited()
    accepted = [_row(engine, delivery_id) for delivery_id in delivery_ids]
    assert {row["state"] for row in accepted} == {"accepted"}
    assert {row["turn_id"] for row in accepted} == {turn_id}
    assert len({row["message_id"] for row in accepted}) == 1
    assert [row["turn_position"] for row in accepted] == [1, 2]
    assert active_context.platform_specific["accepted_agent_run_ids"] == [run_id]


def test_missing_restart_evidence_keeps_unknown_without_resteer(
    managers,
) -> None:
    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _ = asyncio.run(_activate(first))
    first._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    steer_calls = 0
    reconciliation_calls = 0

    async def unknown(_backend, _request):
        nonlocal steer_calls
        steer_calls += 1
        return steer_result(SteerOutcome.UNKNOWN, reason="evidence_unavailable")

    first._steer = unknown
    outcome = asyncio.run(
        first.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p1", content="still unknown"),
            context=_context(),
        )
    )
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


def test_definitive_p1_refusal_stays_behind_the_started_fifo_head(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    turn_id, _ = asyncio.run(_activate(manager))
    older = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="older backlog"),
            context=_context(),
        )
    )
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
        release.set()
        return await pending

    fallback = asyncio.run(race())
    older_row = _row(engine, str(older.delivery_id))
    fallback_row = _row(engine, str(fallback.delivery_id))
    assert older_row["state"] == "claimed"
    assert fallback_row["state"] == "queued"
    assert [text for _turn, text in starts].count("older backlog") == 1


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


def test_definitive_p0_refusal_leaves_backlog_behind_the_active_turn(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        queued = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="backlog"),
            context=_context(),
        )
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
        return str(queued.delivery_id), turn_id

    queued_id, turn_id = asyncio.run(run())
    with engine.connect() as conn:
        active = delivery_store.active_turn(conn, "ses_fsm")
    assert active is not None and active["id"] == turn_id
    assert _row(engine, queued_id)["state"] == "queued"


def test_refused_replacement_releases_its_control_ownership(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, str]:
        turn_id, context = await _activate(manager)
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
                DeliveryRequest(
                    session_id="ses_fsm",
                    priority="p0",
                    content="replacement",
                ),
                context=_context(),
            )
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
        assert result.state == "queued"
        return turn_id, str(result.delivery_id)

    turn_id, replacement_delivery_id = asyncio.run(run())
    with engine.connect() as conn:
        active = delivery_store.get_turn(conn, turn_id)
    assert active is not None
    assert active["state"] == "active"
    assert active["control_state"] == "refused"
    assert active["control_mode"] is None
    assert active["control_successor_turn_id"] is None
    assert active["control_successor_delivery_id"] is None
    assert _row(engine, replacement_delivery_id)["state"] == "queued"


def test_empty_p0_uses_the_control_slot_without_creating_a_message_delivery(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(
            task=holder,
            context=context,
            logical_turn_id=turn_id,
        )
        before = {row["id"] for row in _rows(engine)}
        try:
            result = await manager.deliver(
                DeliveryRequest(session_id="ses_fsm", priority="p0", content=None),
                context=context,
            )
            assert result.state == "waiting_terminal"
            with engine.connect() as conn:
                controlled = delivery_store.get_turn(conn, turn_id)
            assert controlled is not None
            assert controlled["control_mode"] == "stop_only"
            assert {row["id"] for row in _rows(engine)} == before
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    asyncio.run(run())


def test_stop_cancels_a_starting_turn_before_native_write(managers, monkeypatch) -> None:
    """MESSAGE-DELIVERY-022: Stop retires a definitively unwritten input."""

    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    manager.controller.emit_agent_message = AsyncMock()
    settle_calls: list[tuple[list[str], str]] = []

    def settle_runs(run_ids, *, settled_by):
        settle_calls.append((run_ids, settled_by))

    manager.controller.scheduled_task_service = SimpleNamespace(
        settle_agent_runs_without_result=settle_runs,
    )
    dispatch_entered = asyncio.Event()
    stop_intent_seen: list[bool] = []

    async def blocked_prewrite_dispatch(_controller, dispatch_context, *_args, **_kwargs):
        dispatch_evidence = set_dispatch_phase(
            dispatch_context,
            DISPATCH_PHASE_PREWRITE,
        )
        dispatch_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stop_intent_seen.append(
                prewrite_user_stop_requested(dispatch_context)
            )
            # OpenCode absorbs cancellation after cleaning up its inner request
            # task, so lock that adapter behavior into the shared Stop contract.
            return TurnDispatchOutcome(
                error=None,
                settled_by=None,
                backend_dispatch_attempted=False,
            )

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        blocked_prewrite_dispatch,
    )

    async def run() -> tuple[dict, str, str]:
        context = _context()
        admitted = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="stop before native write",
            ),
            context=context,
        )
        assert admitted.turn_id and admitted.delivery_id
        await asyncio.wait_for(dispatch_entered.wait(), timeout=1.0)
        manager.in_flight["ses_fsm"].context.platform_specific[
            "task_execution_id"
        ] = "run-stop-prewrite"
        stopped = await manager.cancel("ses_fsm")
        return stopped, str(admitted.turn_id), str(admitted.delivery_id)

    stopped, turn_id, delivery_id = asyncio.run(run())

    assert stopped == {
        "ok": True,
        "session_id": "ses_fsm",
        "status": "stale_released",
        "reason": "prewrite_canceled",
    }
    assert "ses_fsm" not in manager.in_flight
    manager.controller.command_handler.handle_stop.assert_not_awaited()
    manager.controller.emit_agent_message.assert_not_awaited()
    assert stop_intent_seen == [True]
    assert settle_calls == [(["run-stop-prewrite"], SETTLED_BY_STOPPED)]
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "not_written"
    assert turn["settled_by"] == SETTLED_BY_STOPPED
    retired = _row(engine, delivery_id)
    assert retired["state"] == "retired"
    history = json.loads(retired["delivery_history_json"])["events"]
    assert history[-1]["outcome"] == "canceled"


def test_prewrite_replacement_returns_the_successor_delivery_state(
    managers,
    monkeypatch,
) -> None:
    """A prewrite P0 replacement reports the state of its own successor."""

    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    manager.controller.emit_agent_message = AsyncMock()
    dispatch_entered = asyncio.Event()

    async def blocked_prewrite_dispatch(_controller, dispatch_context, *_args, **_kwargs):
        set_dispatch_phase(dispatch_context, DISPATCH_PHASE_PREWRITE)
        dispatch_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return TurnDispatchOutcome(
                error=None,
                settled_by=None,
                backend_dispatch_attempted=False,
            )

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        blocked_prewrite_dispatch,
    )

    async def run() -> DeliveryResult:
        admitted = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="replace before native write",
            ),
            context=_context(),
        )
        assert admitted.turn_id
        await asyncio.wait_for(dispatch_entered.wait(), timeout=1.0)
        manager._start_persisted_turn = AsyncMock(return_value=True)
        return await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replacement",
            ),
            context=_context(),
        )

    replacement = asyncio.run(run())

    assert replacement.delivery_id is not None
    assert replacement.state == "claimed"
    assert replacement.admission == "started"
    with engine.connect() as conn:
        row = delivery_store.get_delivery(conn, replacement.delivery_id)
    assert row is not None
    assert row["state"] == replacement.state
    assert row["turn_id"] == replacement.turn_id


def test_empty_p0_terminal_race_still_resumes_the_queued_head(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="backlog"),
            context=_context(),
        )
    )
    original_deliver = manager.deliver

    async def terminal_race(request, *, context=None):
        if request.priority == "p0" and request.content is None:
            terminal = manager._terminalize_durable_turn(
                turn_id,
                "failed",
                settled_by="terminal_result",
                evidence_kind="terminal_before_stop_claim",
            )
            assert terminal["changed"] is True
        return await original_deliver(request, context=context)

    monkeypatch.setattr(manager, "deliver", terminal_race)
    result = asyncio.run(manager.cancel("ses_fsm"))

    assert result["ok"] is True
    assert result["status"] == "stale_released"
    asyncio.run(manager._resume_post_terminal("ses_fsm"))
    assert _row(engine, str(queued.delivery_id))["state"] == "claimed"
    assert [text for _turn, text in starts].count("backlog") == 1


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


def test_stopped_runner_starts_successor_already_activated_by_terminal_result(
    managers, monkeypatch
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, list[str]]:
        turn_id, context = await _activate(manager)
        dispatch_started = asyncio.Event()
        dispatch_blocked = asyncio.Event()

        async def blocked_dispatch(*_args, **_kwargs):
            dispatch_started.set()
            await dispatch_blocked.wait()
            return TurnDispatchOutcome(
                error=None,
                settled_by=SETTLED_BY_STOPPED,
                backend_dispatch_attempted=True,
            )

        monkeypatch.setattr(
            "core.session_turns.dispatch_turn_with_outcome",
            blocked_dispatch,
        )
        await SessionTurnManager._run(
            manager,
            "ses_fsm",
            context,
            "primary",
            logical_turn_id=turn_id,
            durable_preallocated=True,
        )
        await dispatch_started.wait()

        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="successor"),
            context=_context(),
        )
        assert admitted.state == "waiting_terminal"
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        assert old is not None
        successor_id = str(old["control_successor_turn_id"])

        manager._finish_durable_terminal_result(
            "ses_fsm",
            turn_id,
            is_error=False,
            settled_by=SETTLED_BY_STOPPED,
        )
        with engine.connect() as conn:
            successor = delivery_store.get_turn(conn, successor_id)
        assert successor is not None and successor["state"] == "starting"

        started: list[str] = []

        async def record_start(candidate: str, **_kwargs) -> bool:
            started.append(candidate)
            return True

        monkeypatch.setattr(manager, "_start_persisted_turn", record_start)
        current = manager.in_flight["ses_fsm"]
        dispatch_blocked.set()
        await current.task
        return successor_id, started

    successor_id, started = asyncio.run(run())
    assert started == [successor_id]


@pytest.mark.parametrize("cancel_holder", [False, True])
def test_stopped_agent_initiated_holder_starts_already_activated_successor(
    managers, monkeypatch, cancel_holder
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, list[str]]:
        context = _context()
        assert manager.register_agent_initiated_turn(context) is True
        await asyncio.sleep(0)
        turn_id = str(context.platform_specific["turn_token"])

        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="successor"),
            context=_context(),
        )
        assert admitted.state == "waiting_terminal"
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        assert old is not None
        successor_id = str(old["control_successor_turn_id"])

        manager._finish_durable_terminal_result(
            "ses_fsm",
            turn_id,
            is_error=False,
            settled_by=SETTLED_BY_STOPPED,
        )
        with engine.connect() as conn:
            successor = delivery_store.get_turn(conn, successor_id)
        assert successor is not None and successor["state"] == "starting"

        started: list[str] = []

        async def record_start(candidate: str, **_kwargs) -> bool:
            started.append(candidate)
            return True

        monkeypatch.setattr(manager, "_start_persisted_turn", record_start)
        holder = manager.in_flight["ses_fsm"].task
        sink = manager.get_turn_sink(manager.controller._get_session_key(context))
        assert sink is not None
        if cancel_holder:
            holder.cancel()
        else:
            sink["settled_by"] = SETTLED_BY_STOPPED
            sink["done_event"].set()
        await asyncio.gather(holder, return_exceptions=True)
        return successor_id, started

    successor_id, started = asyncio.run(run())
    assert started == [successor_id]


def test_unrelated_runner_cancellation_does_not_drain_queued_work(
    managers, monkeypatch
) -> None:
    manager, _restarted, engine, _engine_b, starts = managers

    async def run() -> str:
        turn_id, context = await _activate(manager)
        dispatch_started = asyncio.Event()

        async def blocked_dispatch(*_args, **_kwargs):
            dispatch_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            "core.session_turns.dispatch_turn_with_outcome",
            blocked_dispatch,
        )
        await SessionTurnManager._run(
            manager,
            "ses_fsm",
            context,
            "primary",
            logical_turn_id=turn_id,
            durable_preallocated=True,
        )
        await dispatch_started.wait()

        queued = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="backlog"),
            context=_context(),
        )
        current = manager.in_flight["ses_fsm"]
        current.task.cancel()
        await asyncio.gather(current.task, return_exceptions=True)
        return str(queued.delivery_id)

    delivery_id = asyncio.run(run())
    assert _row(engine, delivery_id)["state"] == "queued"
    assert [text for _turn, text in starts].count("backlog") == 0


def test_adapter_not_active_race_resumes_exact_linked_successor(
    managers, monkeypatch
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, list[str], str]:
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

        manager.controller.command_handler.handle_stop = not_active
        original_terminalize = manager._terminalize_durable_turn
        competing_terminal_written = False

        def terminalize_with_competing_result(candidate, outcome, **kwargs):
            nonlocal competing_terminal_written
            if kwargs.get("settled_by") == "adapter_not_active":
                assert competing_terminal_written is False
                competing_terminal_written = True
                won = original_terminalize(
                    candidate,
                    "canceled",
                    settled_by=SETTLED_BY_STOPPED,
                    evidence_kind="competing_terminal_result",
                )
                assert won["successor_turn_id"]
            return original_terminalize(candidate, outcome, **kwargs)

        monkeypatch.setattr(
            manager,
            "_terminalize_durable_turn",
            terminalize_with_competing_result,
        )
        started: list[str] = []

        async def record_start(candidate: str, **_kwargs) -> bool:
            started.append(candidate)
            return True

        monkeypatch.setattr(manager, "_start_persisted_turn", record_start)
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="successor"),
            context=_context(),
        )
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        assert old is not None
        return str(old["control_successor_turn_id"]), started, admitted.state

    successor_id, started, state = asyncio.run(run())
    assert state == "claimed"
    assert started == [successor_id]


def test_adapter_not_active_runner_cleanup_starts_linked_successor_once(
    managers, monkeypatch
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers

    async def run() -> tuple[str, list[str]]:
        turn_id, context = await _activate(manager)
        dispatch_started = asyncio.Event()

        async def blocked_dispatch(*_args, **_kwargs):
            dispatch_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            "core.session_turns.dispatch_turn_with_outcome",
            blocked_dispatch,
        )
        await SessionTurnManager._run(
            manager,
            "ses_fsm",
            context,
            "primary",
            logical_turn_id=turn_id,
            durable_preallocated=True,
        )
        await dispatch_started.wait()

        async def not_active(stop_context):
            stop_context.platform_specific["stop_failure_reason"] = "not_active"
            return False

        manager.controller.command_handler.handle_stop = not_active
        original_terminalize = manager._terminalize_durable_turn
        competing_terminal_written = False

        def terminalize_with_competing_result(candidate, outcome, **kwargs):
            nonlocal competing_terminal_written
            if kwargs.get("settled_by") == "adapter_not_active":
                assert competing_terminal_written is False
                competing_terminal_written = True
                won = original_terminalize(
                    candidate,
                    "canceled",
                    settled_by=SETTLED_BY_STOPPED,
                    evidence_kind="competing_terminal_result",
                )
                assert won["successor_turn_id"]
            return original_terminalize(candidate, outcome, **kwargs)

        monkeypatch.setattr(
            manager,
            "_terminalize_durable_turn",
            terminalize_with_competing_result,
        )
        started: list[str] = []
        successor_holder: asyncio.Task | None = None

        async def record_successor_start(
            session_id, start_context, _text, *, logical_turn_id=None, **_kwargs
        ):
            nonlocal successor_holder
            assert logical_turn_id is not None
            started.append(logical_turn_id)
            successor_holder = asyncio.create_task(asyncio.Event().wait())
            manager.in_flight[session_id] = Turn(
                task=successor_holder,
                context=start_context,
                logical_turn_id=logical_turn_id,
            )
            _complete_capture_admission(start_context)

        manager._run = record_successor_start
        admitted = await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p0", content="successor"),
            context=_context(),
        )
        with engine.connect() as conn:
            old = delivery_store.get_turn(conn, turn_id)
        assert old is not None
        successor_id = str(old["control_successor_turn_id"])
        assert admitted.state == "claimed"
        if successor_holder is not None:
            successor_holder.cancel()
            await asyncio.gather(successor_holder, return_exceptions=True)
        return successor_id, started

    successor_id, started = asyncio.run(run())
    assert started == [successor_id]


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


def _capture_lost_turn_report(manager: SessionTurnManager) -> tuple[list, list]:
    """Record what a lost turn reports outward, without a real IM client."""

    emitted: list[tuple[str, str]] = []
    stamped: list[tuple[str, str]] = []

    async def _emit(_context, kind, text, **_kwargs):
        emitted.append((kind, text))
        # The dispatcher answers with the delivered message id; a falsy answer
        # means the send failed, which the report path must not read as success.
        return f"msg-{len(emitted)}"

    async def _stamp(_context, message_id, emoji):
        stamped.append((message_id, emoji))
        return True

    manager.controller.emit_agent_message = _emit
    manager.controller.processing_indicator = SimpleNamespace(
        stamp_orphaned_terminal_reaction=_stamp
    )
    return emitted, stamped


def test_lost_im_turn_without_run_reports_interruption(managers) -> None:
    # An IM turn owns no agent_runs row, so the Harness interruption lane cannot
    # reach it: without this report the thread just stops, which is
    # indistinguishable from an agent choosing to stay quiet.
    first, restarted, engine, _engine_b, _starts = managers
    context = _context()
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="im turn killed by restart",
                native_message_id="m-origin",
            ),
            context=context,
        )
    )
    turn_id = str(admitted.turn_id)
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    first._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    first.on_native_start(
        context,
        backend="codex",
        runtime_key=f"runtime-key-{turn_id}",
        runtime_turn_id=f"runtime-{turn_id}",
    )
    emitted, stamped = _capture_lost_turn_report(restarted)
    restarted._active_identity = lambda *_args: None

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm", service_restart=True))

    with engine.connect() as conn:
        settled = delivery_store.get_turn(conn, turn_id)
    assert settled is not None and settled["terminal_evidence_kind"] == "restart_runtime_missing"
    assert [kind for kind, _text in emitted] == ["notify"]
    assert "interrupted" in emitted[0][1].lower()
    # The dead process could not clear its own 👀; recovery retires it in place.
    assert stamped == [("m-origin", INTERRUPTED_REACTION_EMOJI)]


def test_lost_quick_reply_turn_stamps_the_echo_it_reacted_on(managers) -> None:
    """A quick-reply turn wears its 👀 on the bot echo, not on a user message.

    The callback is admitted with ``native_message_id=None`` on purpose (it would
    collide with platform event dedup) and the echo id survives only in the
    durable admission context. Reading the native id alone yields ``""`` here, so
    the ⚠️ is skipped and the echo keeps claiming the turn is still running —
    exactly the stuck indicator the report exists to retire.
    """

    first, restarted, _engine, _engine_b, _starts = managers
    context = _context()
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="quick reply killed by restart",
                admission_context={"processing_indicator_message_id": "echo-9"},
            ),
            context=context,
        )
    )
    turn_id = str(admitted.turn_id)
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    first._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    first.on_native_start(
        context,
        backend="codex",
        runtime_key=f"runtime-key-{turn_id}",
        runtime_turn_id=f"runtime-{turn_id}",
    )
    emitted, stamped = _capture_lost_turn_report(restarted)
    restarted._active_identity = lambda *_args: None

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm", service_restart=True))

    assert [kind for kind, _text in emitted] == ["notify"]
    assert stamped == [("echo-9", INTERRUPTED_REACTION_EMOJI)]


def test_lost_im_turn_report_waits_for_its_transport(managers) -> None:
    # Recovery runs at startup, BEFORE the IM transports finish connecting.
    # Emitting then would drop the notice into a client that cannot send, so the
    # report is held and flushed from the transport-ready hook instead.
    first, restarted, _engine, _engine_b, _starts = managers
    context = _context()
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="im turn killed before the transport came up",
                native_message_id="m-origin",
            ),
            context=context,
        )
    )
    turn_id = str(admitted.turn_id)
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    first._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    first.on_native_start(
        context,
        backend="codex",
        runtime_key=f"runtime-key-{turn_id}",
        runtime_turn_id=f"runtime-{turn_id}",
    )
    emitted, stamped = _capture_lost_turn_report(restarted)
    restarted._active_identity = lambda *_args: None
    ready: set[str] = set()
    restarted._transport_can_deliver = lambda platform: platform in ready

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm", service_restart=True))

    assert emitted == [] and stamped == []

    ready.add("avibe")
    reported = asyncio.run(restarted.notify_transport_ready("avibe"))

    assert reported == 1
    assert [kind for kind, _text in emitted] == ["notify"]
    assert stamped == [("m-origin", INTERRUPTED_REACTION_EMOJI)]
    # The queue is drained, not replayed: a second hook fires nothing.
    assert asyncio.run(restarted.notify_transport_ready("avibe")) == 0
    assert len(emitted) == 1


def test_lost_im_turn_report_survives_a_failed_send(managers) -> None:
    # "Transport ready" is the transport's claim, not a delivered message. The
    # dispatcher swallows a send failure and answers None, and the turn is
    # already terminal, so treating that as done would discard the only account
    # of the interruption the user will ever get.
    first, restarted, _engine, _engine_b, _starts = managers
    context = _context()
    admitted = asyncio.run(
        first.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="im turn killed by restart",
                native_message_id="m-origin",
            ),
            context=context,
        )
    )
    turn_id = str(admitted.turn_id)
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    first._active_identity = lambda _backend, _session_id, logical_id: (
        logical_id,
        f"native-{logical_id}",
    )
    first.on_native_start(
        context,
        backend="codex",
        runtime_key=f"runtime-key-{turn_id}",
        runtime_turn_id=f"runtime-{turn_id}",
    )
    emitted, stamped = _capture_lost_turn_report(restarted)
    restarted._active_identity = lambda *_args: None
    delivers = False

    async def _emit(_context, kind, text, **_kwargs):
        emitted.append((kind, text))
        return "msg-1" if delivers else None

    restarted.controller.emit_agent_message = _emit

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm", service_restart=True))

    # It was attempted and it failed: no ⚠️ next to a notice nobody received.
    assert [kind for kind, _text in emitted] == ["notify"]
    assert stamped == []

    delivers = True
    assert asyncio.run(restarted.notify_transport_ready("avibe")) == 1
    assert len(emitted) == 2
    assert stamped == [("m-origin", INTERRUPTED_REACTION_EMOJI)]


def test_retained_lost_turn_report_is_retried_on_its_own_clock(managers) -> None:
    # notify_transport_ready has exactly one caller (_on_im_ready) and the IM
    # client suppresses repeat ready callbacks until the platform goes unready,
    # so a connected transport that merely hit one API error would hold the
    # notice forever. The retry has to come from the manager itself.
    _first, restarted, _engine, _engine_b, _starts = managers
    emitted, _stamped = _capture_lost_turn_report(restarted)
    restarted.LOST_TURN_RETRY_DELAYS = (0.0, 0.0)
    restarted._pending_lost_turn_reports["slack"] = [("ses_fsm", "m-origin")]
    failures = 1

    async def _emit(_context, kind, text, **_kwargs):
        nonlocal failures
        emitted.append((kind, text))
        if failures:
            failures -= 1
            return None
        return "msg-late"

    restarted.controller.emit_agent_message = _emit
    restarted._delivery_context = lambda _session_id: _context()

    async def _flush_then_settle():
        assert await restarted.notify_transport_ready("slack") == 0
        assert restarted._pending_lost_turn_reports["slack"]
        # Nothing else will call in; the scheduled retry is the only hope.
        task = restarted._lost_turn_retry_tasks["slack"]
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_flush_then_settle())

    assert len(emitted) == 2
    assert not restarted._pending_lost_turn_reports.get("slack")
    assert not restarted._lost_turn_retry_tasks


def test_lost_turn_retry_gives_up_instead_of_spinning(managers) -> None:
    # A hard outage must not turn into an unbounded resend loop; the next real
    # reconnect flushes whatever is still owed.
    _first, restarted, _engine, _engine_b, _starts = managers
    emitted, _stamped = _capture_lost_turn_report(restarted)
    restarted.LOST_TURN_RETRY_DELAYS = (0.0, 0.0)
    restarted._pending_lost_turn_reports["slack"] = [("ses_fsm", "m-origin")]

    async def _emit(_context, kind, text, **_kwargs):
        emitted.append((kind, text))
        return None

    restarted.controller.emit_agent_message = _emit
    restarted._delivery_context = lambda _session_id: _context()

    async def _flush_and_exhaust():
        await restarted.notify_transport_ready("slack")
        await asyncio.wait_for(restarted._lost_turn_retry_tasks["slack"], timeout=5)

    asyncio.run(_flush_and_exhaust())

    # One initial attempt plus one per configured delay, then it stops.
    assert len(emitted) == 3
    assert restarted._pending_lost_turn_reports["slack"] == [("ses_fsm", "m-origin")]


def test_lost_turn_owning_a_run_leaves_the_notice_to_the_harness_lane(managers) -> None:
    # A Harness turn already gets harness.run.interrupted.* stamped on its Run.
    # Reporting again here would double-notify the same interruption.
    first, restarted, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(first, text="harness turn"))
    with engine.begin() as conn:
        accepted = delivery_store.delivery_for_turn(conn, turn_id)
        assert accepted is not None
        conn.execute(
            agent_runs.insert().values(
                id="run-lost-turn",
                definition_id=None,
                run_type="scheduled",
                status="running",
                cancel_requested=0,
                session_id="ses_fsm",
                delivery_id=str(accepted["id"]),
                created_at="2026-08-01T00:00:00Z",
                updated_at="2026-08-01T00:00:00Z",
                metadata_json="{}",
            )
        )
    assert first.accepted_agent_run_ids_for_turn(turn_id) == ["run-lost-turn"]
    emitted, stamped = _capture_lost_turn_report(restarted)
    restarted._active_identity = lambda *_args: None

    asyncio.run(restarted.recover_durable_delivery_state("ses_fsm", service_restart=True))

    with engine.connect() as conn:
        settled = delivery_store.get_turn(conn, turn_id)
    assert settled is not None and settled["terminal_evidence_kind"] == "restart_runtime_missing"
    assert emitted == []
    assert stamped == []


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
        _complete_capture_admission(_context_value)

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


@pytest.mark.anyio
async def test_permanent_start_rejection_retires_delivery_without_retry(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    admitted = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="invalid payload"),
        context=_context(),
    )
    following = await manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue FIFO"),
        context=_context(),
    )
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, str(admitted.turn_id))
    assert turn is not None
    attempt_id = str(turn["start_attempt_id"])

    assert manager.settle_start_attempt_invalid_input(
        str(admitted.turn_id),
        attempt_id,
        backend="opencode",
    )
    delivery = _row(engine, str(admitted.delivery_id))
    assert delivery["state"] == "retired"
    history = json.loads(delivery["delivery_history_json"])["events"]
    assert history[-1]["outcome"] == "invalid_input"
    assert _row(engine, str(following.delivery_id))["state"] == "claimed"
    assert len(starts) == 1
    with engine.connect() as conn:
        terminal = delivery_store.get_turn(conn, str(admitted.turn_id))
        assert delivery_store.claimable_fifo_head(conn, "ses_fsm") is None
    assert terminal is not None
    assert terminal["terminal_outcome"] == "not_written"

    await asyncio.sleep(0)
    assert _row(engine, str(following.delivery_id))["state"] == "claimed"
    assert starts[-1][1] == "continue FIFO"


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


def test_persisted_start_does_not_acquire_lifecycle_admission(
    managers,
    monkeypatch,
) -> None:
    """Scenario: MEMORY-INDEP-001. Dispatch must not wait on Memory."""

    manager, _other, _engine, _engine_b, _starts = managers
    acquired = False
    original_acquire = manager.acquire_lifecycle_admission

    async def track_acquire(raw_session_id):
        nonlocal acquired
        acquired = True
        return await original_acquire(raw_session_id)

    monkeypatch.setattr(manager, "acquire_lifecycle_admission", track_acquire)
    asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="do not fence dispatch",
            ),
            context=_context(),
        )
    )

    assert acquired is False
    assert "ses_fsm" not in manager._session_lifecycle_states


@pytest.mark.parametrize(
    ("trigger_kind", "definition_id"),
    [
        ("scheduled", "task_123"),
        ("watch", "watch_123"),
        ("webhook", "webhook_123"),
        ("hook", None),
        ("agent_run", None),
        ("activity_recovery", None),
    ],
)
def test_scheduled_submit_preserves_trigger_provenance_after_prewrite_failure(
    managers,
    monkeypatch,
    trigger_kind,
    definition_id,
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

    context = _context()
    context.platform_specific.update(
        {
            "task_trigger_kind": trigger_kind,
            "task_definition_id": definition_id,
        }
    )
    result = asyncio.run(
        manager.submit(
            "ses_fsm",
            context,
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
    payload = delivery_store.delivery_payload(rows[0])
    assert payload["author_name"] == trigger_kind
    assert payload["author_id"] == definition_id
    assert ("queue.updated", {"session_id": "ses_fsm"}) in published


def test_terminal_agent_run_wins_after_start_claim_before_native_dispatch(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    surviving_delivery_id = delivery_store.new_delivery_id()
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
        surviving = delivery_store.insert_delivery(
            conn,
            delivery_id=surviving_delivery_id,
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
                text="surviving batch input",
            ),
            dispatch_text="surviving batch input",
        )
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="codex",
            deliveries=[delivery, surviving],
            dispatch_text="must not run\n\nsurviving batch input",
            attempt_id=attempt_id,
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(delivery_id=delivery_id)
        )

    assert asyncio.run(manager._start_persisted_turn(turn_id, context=_context())) is False
    assert [text for _turn_id, text in starts] == ["surviving batch input"]
    assert _row(engine, delivery_id)["state"] == "retired"
    surviving_after = _row(engine, surviving_delivery_id)
    assert surviving_after["state"] == "claimed"
    assert surviving_after["turn_id"] != turn_id
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
            select(agent_sessions.c.agent_status).where(
                agent_sessions.c.id == "ses_fsm"
            )
        ).scalar_one()
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_outcome"] == "failed"
    assert turn["terminal_evidence_kind"] == "backend_refresh_start_failed"
    assert status == "failed"


def test_backend_refresh_defers_successor_activated_by_old_turn_cancellation(
    managers,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    manager._run = SessionTurnManager._run.__get__(manager, SessionTurnManager)
    native_started = asyncio.Event()

    async def long_native_turn(_controller, context, *_args, **_kwargs):
        logical_turn_id = str(context.platform_specific["turn_token"])
        context.platform_specific["agent_runtime_turn_token"] = (
            f"runtime-{logical_turn_id}"
        )
        manager._active_identity = lambda _backend, _session_id, logical_id: (
            logical_id,
            f"native-{logical_id}",
        )
        manager.on_native_start(
            context,
            backend="codex",
            runtime_key=f"runtime-key-{logical_turn_id}",
            runtime_turn_id=f"runtime-{logical_turn_id}",
        )
        native_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "core.session_turns.dispatch_turn_with_outcome",
        long_native_turn,
    )

    async def run() -> tuple[str, str, str]:
        admitted = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="old backend work",
            ),
            context=_context(),
        )
        assert admitted.turn_id
        await asyncio.wait_for(native_started.wait(), timeout=3)
        replacement = await manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p0",
                content="replacement after refresh",
            ),
            context=_context(),
        )
        assert replacement.state == "waiting_terminal"
        with engine.connect() as conn:
            old_turn = delivery_store.get_turn(conn, str(admitted.turn_id))
        assert old_turn is not None and old_turn["control_successor_turn_id"]
        successor_turn_id = str(old_turn["control_successor_turn_id"])

        manager.begin_backend_drain("codex")
        released = await manager.release_for_backend_refresh(
            backend="codex",
            base_session_ids={"ses_fsm"},
        )
        assert released == 1
        return str(admitted.turn_id), successor_turn_id, str(replacement.delivery_id)

    old_turn_id, successor_turn_id, replacement_delivery_id = asyncio.run(run())

    with engine.connect() as conn:
        old_turn = delivery_store.get_turn(conn, old_turn_id)
        successor = delivery_store.get_turn(conn, successor_turn_id)
        replacement = delivery_store.get_delivery(conn, replacement_delivery_id)
    assert old_turn is not None and old_turn["state"] == "terminal"
    assert successor is not None and successor["state"] == "terminal"
    assert replacement is not None and replacement["state"] == "queued"
    assert manager._deferred_restart_sessions == {"codex": {"ses_fsm"}}


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
    assert message["created_at"] == messages_service.canonical_message_timestamp(
        delivery["submitted_at"]
    )
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


@pytest.mark.anyio
async def test_concurrent_im_attachment_retry_keeps_one_reserved_owner(
    managers,
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    first_path = tmp_path / "first.txt"
    retry_path = tmp_path / "retry.txt"
    first_path.write_text("first", encoding="utf-8")
    retry_path.write_text("retry", encoding="utf-8")
    controller = SimpleNamespace(
        config=SimpleNamespace(language="en"),
        im_client=SimpleNamespace(formatter=None),
        settings_manager=SimpleNamespace(sessions=SimpleNamespace()),
        session_manager=SimpleNamespace(),
        receiver_tasks={},
    )
    handler = MessageHandler(controller)
    handler.set_session_handler(
        SimpleNamespace(ensure_agent_session_id=lambda *_args, **_kwargs: "ses_fsm")
    )
    monkeypatch.setattr("storage.db.get_cached_sqlite_engine", lambda: engine)

    def context() -> MessageContext:
        return MessageContext(
            user_id="U1",
            channel_id="C1",
            message_id="native-attachment-1",
            platform="slack",
        )

    async def admit(path: Path) -> None:
        await handler._admit_human_delivery(
            manager=manager,
            context=context(),
            dispatch_text="review attachment",
            display_text="review attachment",
            processed_files=[
                FileAttachment(
                    name=path.name,
                    mimetype="text/plain",
                    local_path=str(path),
                    size=path.stat().st_size,
                )
            ],
            session_key="slack::C1",
            agent_name="codex",
            session_anchor="base-session",
            working_path="/tmp",
            vibe_agent=None,
            delivery_intent="steer",
            downloaded_attachment_paths=[str(path)],
            admission_context={},
        )

    await admit(first_path)
    await admit(retry_path)

    with engine.connect() as conn:
        deliveries = conn.execute(select(message_deliveries)).mappings().all()
        media = conn.execute(select(media_objects)).mappings().all()
    assert len(deliveries) == 1
    assert len(media) == 1
    assert media[0]["local_path"] == str(first_path)
    assert first_path.exists()
    assert not retry_path.exists()


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
                message_kind="original",
            ),
            dispatch_text="",
        )
    dispatched: list[tuple[str, list[str], str, bool, bool]] = []

    async def capture(_session_id, context, text, **_kwargs):
        dispatched.append(
            (
                text,
                [str(item.local_path) for item in (context.files or [])],
                context.message_kind,
                context.is_original_human_text is True,
                context.is_original_human_attachment is True,
            )
        )
        _complete_capture_admission(context)

    manager._run = capture
    asyncio.run(manager.recover_durable_delivery_state())

    assert dispatched == [("", [str(attachment)], "original", True, True)]
    assert _row(engine, delivery_id)["state"] == "claimed"


@pytest.mark.parametrize("invalid_reference", ["missing", "revoked", "wrong_session"])
def test_reserved_attachment_only_submission_requires_a_resolvable_reference(
    managers,
    tmp_path: Path,
    invalid_reference: str,
) -> None:
    from storage import media_service

    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    token = "missing-attachment-token"
    if invalid_reference == "wrong_session":
        _seed_session(engine, "ses_other")
    with engine.begin() as conn:
        if invalid_reference != "missing":
            attachment = tmp_path / f"{invalid_reference}.txt"
            attachment.write_text("invalid durable attachment", encoding="utf-8")
            attachment_session_id = "ses_fsm"
            if invalid_reference == "wrong_session":
                attachment_session_id = "ses_other"
            token = media_service.register(
                conn,
                scope_id=None,
                session_id=attachment_session_id,
                kind="file",
                source="user_upload",
                local_path=str(attachment),
                file_name=attachment.name,
                content_type="text/plain",
            )
            if invalid_reference == "revoked":
                conn.execute(
                    update(media_objects)
                    .where(media_objects.c.token == token)
                    .values(revoked_at="2026-08-01T00:00:01Z")
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
        conn.execute(
            agent_runs.insert().values(
                id=f"run_invalid_attachment_{invalid_reference}",
                run_type="agent",
                status="queued",
                session_id="ses_fsm",
                delivery_id=delivery_id,
                cancel_requested=0,
                created_at="2026-08-01T00:00:00Z",
                updated_at="2026-08-01T00:00:00Z",
                metadata_json="{}",
            )
        )

    assert asyncio.run(manager.recover_durable_delivery_state()) == ["ses_fsm"]

    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        run = conn.execute(
            select(agent_runs).where(
                agent_runs.c.id == f"run_invalid_attachment_{invalid_reference}"
            )
        ).mappings().one()
    assert run["status"] == "canceled"
    assert run["cancel_requested"] == 1


def test_fifo_claim_retires_invalid_attachment_head_and_starts_next(
    managers,
) -> None:
    manager, _other, engine, _engine_b, starts = managers
    invalid_id = delivery_store.new_delivery_id()
    valid_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=invalid_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="",
                content={"attachments": [{"token": "deleted-token"}]},
            ),
            dispatch_text="",
            now="2026-08-01T00:00:01Z",
        )
        delivery_store.insert_delivery(
            conn,
            delivery_id=valid_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="valid following input",
            ),
            dispatch_text="valid following input",
            now="2026-08-01T00:00:02Z",
        )

    assert asyncio.run(manager.drain_delivery_queue("ses_fsm"))

    assert _row(engine, invalid_id)["state"] == "retired"
    assert _row(engine, valid_id)["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["valid following input"]


def _insert_queued_delivery_with_run(
    engine,
    *,
    delivery_id: str,
    text: str,
    now: str,
    run_id: str | None = None,
    run_status: str = "queued",
    cancel_requested: int = 0,
) -> None:
    with engine.begin() as conn:
        delivery_store.insert_delivery(
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
                text=text,
            ),
            dispatch_text=text,
            now=now,
        )
        if run_id is not None:
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    run_type="agent",
                    status=run_status,
                    session_id="ses_fsm",
                    delivery_id=delivery_id,
                    cancel_requested=cancel_requested,
                    created_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
            )


@pytest.mark.parametrize(
    ("run_status", "cancel_requested"),
    [("failed", 0), ("completed", 0), ("canceled", 0), ("queued", 1)],
)
def test_fifo_claim_retires_head_whose_agent_run_can_no_longer_start(
    managers,
    run_status: str,
    cancel_requested: int,
) -> None:
    """A settled Agent Run must not brick its Session's queue (crash loop)."""

    manager, _other, engine, _engine_b, starts = managers
    poisoned_id = delivery_store.new_delivery_id()
    valid_id = delivery_store.new_delivery_id()
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=poisoned_id,
        text="unexecutable head",
        now="2026-08-01T00:00:01Z",
        run_id="run_settled_before_claim",
        run_status=run_status,
        cancel_requested=cancel_requested,
    )
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=valid_id,
        text="valid following input",
        now="2026-08-01T00:00:02Z",
    )

    assert asyncio.run(manager.drain_delivery_queue("ses_fsm"))

    assert _row(engine, poisoned_id)["state"] == "retired"
    assert _row(engine, valid_id)["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["valid following input"]


def test_recovery_survives_queued_delivery_whose_agent_run_settled(
    managers,
) -> None:
    """Startup recovery must not raise on a Delivery its Run already abandoned."""

    manager, _other, engine, _engine_b, starts = managers
    poisoned_id = delivery_store.new_delivery_id()
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=poisoned_id,
        text="watch fire whose run failed",
        now="2026-08-01T00:00:01Z",
        run_id="run_failed_before_restart",
        run_status="failed",
    )

    assert asyncio.run(manager.recover_durable_delivery_state(service_restart=True)) == []

    assert _row(engine, poisoned_id)["state"] == "retired"
    assert starts == []


def test_recovery_binds_agentless_session_before_draining_queue(managers) -> None:
    """Startup repairs the pre-fix queue state instead of crashing again."""

    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
        delivery_store.insert_delivery(
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
                text="recover the queued turn",
            ),
            dispatch_text="recover the queued turn",
        )
    manager.bind_context(_agentless_context)
    manager.controller.resolve_vibe_agent_for_context = lambda *_args, **_kwargs: SimpleNamespace(
        id="agent-codex",
        name="reviewer",
        backend="codex",
    )

    assert asyncio.run(
        manager.recover_durable_delivery_state(service_restart=True)
    ) == []

    assert _row(engine, delivery_id)["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["recover the queued turn"]
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == ("agent-codex", "reviewer", "codex")


def test_recovery_binds_scheduled_head_from_delivery_provenance(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(
                agent_id=None,
                agent_name=None,
                agent_backend="",
                agent_variant="default",
            )
        )
        delivery_store.insert_delivery(
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
                text="recover scheduled turn",
                metadata={
                    SCHEDULED_PROVENANCE_KEY: {
                        "platform_specific": {
                            "task_trigger_kind": "agent_run",
                            "vibe_agent_id": "agent-scheduled",
                            "vibe_agent_name": "scheduled",
                        }
                    }
                },
            ),
            dispatch_text="recover scheduled turn",
        )
    manager.bind_context(_agentless_context)
    manager.controller.resolve_vibe_agent_for_context = lambda _context, **kwargs: (
        SimpleNamespace(
            id="agent-scheduled",
            name="scheduled",
            backend="claude",
        )
        if kwargs.get("override_agent_name") == "scheduled"
        else None
    )

    assert asyncio.run(manager.recover_durable_delivery_state(service_restart=True)) == []

    assert _row(engine, delivery_id)["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["recover scheduled turn"]
    with engine.connect() as conn:
        binding = conn.execute(
            select(
                agent_sessions.c.agent_id,
                agent_sessions.c.agent_name,
                agent_sessions.c.agent_backend,
            ).where(agent_sessions.c.id == "ses_fsm")
        ).one()
    assert binding == ("agent-scheduled", "scheduled", "claude")


def test_fifo_claim_starts_head_whose_agent_run_is_still_queued(
    managers,
) -> None:
    manager, _other, engine, _engine_b, starts = managers
    delivery_id = delivery_store.new_delivery_id()
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=delivery_id,
        text="claimable input",
        now="2026-08-01T00:00:01Z",
        run_id="run_claimable",
    )

    assert asyncio.run(manager.drain_delivery_queue("ses_fsm"))

    assert _row(engine, delivery_id)["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["claimable input"]
    with engine.connect() as conn:
        run = conn.execute(
            select(agent_runs).where(agent_runs.c.id == "run_claimable")
        ).mappings().one()
    assert run["status"] == "running"


def test_send_now_retires_head_whose_agent_run_can_no_longer_start(
    managers,
) -> None:
    """Explicit promotion must not raise on the same poisoned row startup drain retires."""

    manager, _other, engine, _engine_b, starts = managers
    poisoned_id = delivery_store.new_delivery_id()
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=poisoned_id,
        text="head whose run failed",
        now="2026-08-01T00:00:01Z",
        run_id="run_settled_at_head",
        run_status="failed",
    )

    promoted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content=None,
                expected_delivery_id=poisoned_id,
            ),
            context=_context(),
        )
    )

    assert promoted.state == "refused"
    assert promoted.reason == "stale_head"
    assert _row(engine, poisoned_id)["state"] == "retired"
    assert starts == []


def test_p3_admission_retires_poisoned_backlog_and_still_starts(
    managers,
) -> None:
    """Immediate admission drains past a settled Run instead of raising on it."""

    manager, _other, engine, _engine_b, starts = managers
    poisoned_id = delivery_store.new_delivery_id()
    _insert_queued_delivery_with_run(
        engine,
        delivery_id=poisoned_id,
        text="backlog whose run failed",
        now="2026-08-01T00:00:01Z",
        run_id="run_settled_before_admission",
        run_status="failed",
    )

    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="fresh input"),
            context=_context(),
        )
    )

    assert _row(engine, poisoned_id)["state"] == "retired"
    assert admitted.turn_id
    assert _row(engine, str(admitted.delivery_id))["state"] == "claimed"
    assert [text for _turn_id, text in starts] == ["fresh input"]


def test_final_dispatch_gate_retires_batch_when_attachment_expires_after_claim(
    managers,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.inbox_events import bus
    from storage import media_service

    manager, _other, engine, _engine_b, starts = managers
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(bus, "publish", lambda event, payload: published.append((event, payload)))
    attachment = tmp_path / "expires-after-claim.txt"
    attachment.write_text("expires after claim", encoding="utf-8")
    delivery_id = delivery_store.new_delivery_id()
    with manager._runtime_start_owner("ses_fsm", "codex") as start_owner, engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            kind="file",
            source="user_upload",
            local_path=str(attachment),
            file_name=attachment.name,
            content_type="text/plain",
        )
        delivery_store.insert_delivery(
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
                text="",
                content={"attachments": [{"token": token}]},
            ),
            dispatch_text="",
        )
        turn_id = manager._claim_fifo_batch_in_transaction(
            conn,
            owner=start_owner,
            session_id="ses_fsm",
            backend="codex",
        )
        assert turn_id is not None
    with engine.begin() as conn:
        conn.execute(
            update(media_objects)
            .where(media_objects.c.token == token)
            .values(revoked_at="2026-08-01T00:00:01Z")
        )

    assert not asyncio.run(manager._start_persisted_turn(turn_id))

    assert starts == []
    assert _row(engine, delivery_id)["state"] == "retired"
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["state"] == "terminal"
    assert turn["terminal_evidence_kind"] == "invalid_input_before_native_dispatch"
    assert ("queue.updated", {"session_id": "ses_fsm"}) in published


def test_final_dispatch_gate_preserves_valid_batch_members(
    managers,
    tmp_path: Path,
) -> None:
    from storage import media_service

    manager, _other, engine, _engine_b, starts = managers
    attachment = tmp_path / "expired-batch-member.txt"
    attachment.write_text("expires after claim", encoding="utf-8")
    valid_id = delivery_store.new_delivery_id()
    invalid_id = delivery_store.new_delivery_id()
    with engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            kind="file",
            source="user_upload",
            local_path=str(attachment),
            file_name=attachment.name,
            content_type="text/plain",
        )
        valid = delivery_store.insert_delivery(
            conn,
            delivery_id=valid_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                text="surviving text",
            ),
            dispatch_text="surviving text",
        )
        invalid = delivery_store.insert_delivery(
            conn,
            delivery_id=invalid_id,
            session_id="ses_fsm",
            priority="p3",
            state="queued",
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
        turn_id = delivery_store.new_turn_id()
        delivery_store.claim_start_batch(
            conn,
            turn_id=turn_id,
            session_id="ses_fsm",
            backend="codex",
            deliveries=[valid, invalid],
            dispatch_text="surviving text",
        )
        conn.execute(
            update(media_objects)
            .where(media_objects.c.token == token)
            .values(revoked_at="2026-08-01T00:00:01Z")
        )

    assert not asyncio.run(manager._start_persisted_turn(turn_id))

    assert _row(engine, invalid_id)["state"] == "retired"
    valid_after = _row(engine, valid_id)
    assert valid_after["state"] == "claimed"
    assert valid_after["turn_id"] != turn_id
    assert [text for _turn_id, text in starts] == ["surviving text"]


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


def test_stop_terminalization_resumes_oldest_queued_segment(managers) -> None:
    """MESSAGE-DELIVERY-102: an idle Session cannot retain claimable backlog."""

    manager, other, engine, _engine_b, starts = managers

    async def run() -> None:
        turn_id, context = await _activate(manager)
        await manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="queued-after-stop"),
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

    asyncio.run(run())
    rows = _rows(engine)
    queued = next(row for row in rows if row["dispatch_text"] == "queued-after-stop")
    assert queued["state"] == "claimed"
    assert [text for _, text in starts].count("queued-after-stop") == 1


@pytest.mark.parametrize(
    "settled_by",
    [SETTLED_BY_RESTARTED, SETTLED_BY_STOPPED],
)
def test_canceled_shutdown_runner_preserves_deferred_queue(
    managers,
    settled_by: str,
) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="resume after service restart",
            ),
            context=_context(),
        )
    )
    terminal = manager._terminalize_durable_turn(
        turn_id,
        "canceled" if settled_by == SETTLED_BY_STOPPED else "failed",
        settled_by=settled_by,
        evidence_kind=(
            "service_shutdown_after_user_stop"
            if settled_by == SETTLED_BY_STOPPED
            else "service_shutdown"
        ),
        resume_successors=False,
    )
    assert terminal["changed"] is True

    released = manager._reconcile_durable_runner_release(
        turn_id,
        cancelled=True,
        failed=False,
        prewrite_refused=False,
        definitive_prewrite_exit=False,
        settled_by=settled_by,
        terminal_is_error=True,
        cancel_defers_queue_resume=True,
    )

    assert released["defer_queue_resume"] is True
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"


@pytest.mark.parametrize(
    ("settled_by", "expected_outcome"),
    [
        (SETTLED_BY_BACKEND_REFRESH, "canceled"),
        (SETTLED_BY_RESTARTED, "failed"),
        (SETTLED_BY_NO_TERMINAL_RESULT, "failed"),
    ],
)
def test_cancelled_runner_release_writes_the_settlement_outcome(
    managers,
    settled_by: str,
    expected_outcome: str,
) -> None:
    """The cancellation branch reads the shared Turn-outcome map.

    ``release_for_backend_refresh`` cancels this runner AND terminalizes the
    durable Turns it can reach directly as ``canceled``. When the branch
    hardcoded ``stopped`` as the only non-failure, one rolling refresh landed two
    different outcomes depending on which writer won. ``restarted`` stays
    ``failed`` -- a service shutdown is not a cancellation -- as does the
    pessimistic default a releaser writes when it can name nothing.
    """

    manager, _other, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))

    released = manager._reconcile_durable_runner_release(
        turn_id,
        cancelled=True,
        failed=False,
        prewrite_refused=False,
        definitive_prewrite_exit=False,
        settled_by=settled_by,
        terminal_is_error=True,
        cancel_defers_queue_resume=True,
    )

    assert released["changed"] is True
    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
    assert turn is not None
    assert turn["terminal_outcome"] == expected_outcome
    # Only a user stop releases the queue here; a refresh still defers it to the
    # post-refresh generation, so the outcome word must not move that lever.
    assert released["defer_queue_resume"] is True


def test_ambiguous_start_failure_defers_runner_queue_resume(managers) -> None:
    manager, _other, engine, _engine_b, starts = managers
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="start exactly once",
            ),
            context=_context(),
        )
    )
    assert admitted.turn_id

    released = manager._reconcile_durable_runner_release(
        admitted.turn_id,
        cancelled=False,
        failed=True,
        prewrite_refused=False,
        definitive_prewrite_exit=False,
        settled_by=None,
        terminal_is_error=True,
    )

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, admitted.turn_id)
    assert released["defer_queue_resume"] is True
    assert turn is not None
    assert turn["state"] == "starting"
    assert turn["start_receipt_outcome"] == "unknown"
    assert [text for _turn_id, text in starts] == ["start exactly once"]


def test_open_backlog_starts_oldest_before_new_idle_p3(managers) -> None:
    """An open compatible FIFO segment is claimed as one native Turn."""

    manager, _other, engine, _engine_b, starts = managers
    started_contexts: list[dict[str, object]] = []

    async def capture_start(_session_id, context, text, **kwargs):
        starts.append((str(kwargs.get("logical_turn_id") or ""), text))
        started_contexts.append(dict(context.platform_specific or {}))
        _complete_capture_admission(context)

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


def test_owned_run_sweep_retries_terminal_turn_settlement_before_orphaning(
    managers,
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))
    run_id = "run-terminal-settlement-retry"
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        initial = delivery_store.initial_deliveries_for_turn(conn, turn_id)[0]
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
                delivery_id=initial["id"],
                created_at=now,
                started_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    terminal = manager._terminalize_durable_turn(
        turn_id,
        "completed",
        settled_by="terminal_result",
        evidence_kind="terminal_result",
        evidence={"settles_run": True, "result_text": "done"},
    )
    assert terminal["changed"] is True
    with engine.connect() as conn:
        terminal_turn = delivery_store.get_turn(conn, turn_id)
    assert terminal_turn is not None

    class _FlakyTerminalSettlement:
        calls = 0

        def settle_agent_runs_from_terminal_turn(self, execution_ids, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient settlement failure")
            with engine.begin() as conn:
                conn.execute(
                    update(agent_runs)
                    .where(agent_runs.c.id.in_(list(execution_ids)))
                    .values(status="succeeded", completed_at=now, updated_at=now)
                )

    settlement = _FlakyTerminalSettlement()
    manager.controller.scheduled_task_service = settlement
    manager._settle_agent_run_ids_from_terminal_turn([run_id], terminal_turn)
    with engine.connect() as conn:
        assert conn.execute(
            select(agent_runs.c.status).where(agent_runs.c.id == run_id)
        ).scalar_one() == "running"

    assert run_id not in manager.snapshot_owned_agent_run_ids({run_id})
    assert settlement.calls == 1
    assert run_id not in manager.owned_agent_run_ids()
    assert settlement.calls == 2
    with engine.connect() as conn:
        assert conn.execute(
            select(agent_runs.c.status).where(agent_runs.c.id == run_id)
        ).scalar_one() == "succeeded"


def test_snapshot_retains_pre_turn_delivery_ownership(managers) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers
    run_id = "run-queued-delivery-owner"
    delivery_id = delivery_store.new_delivery_id()
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        delivery_store.insert_delivery(
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
                text="queued owner",
            ),
            dispatch_text="queued owner",
            now=now,
        )
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
                delivery_id=delivery_id,
                created_at=now,
                started_at=now,
                updated_at=now,
                metadata_json="{}",
            )
        )

    assert manager.snapshot_owned_agent_run_ids({run_id}) == {run_id}


@pytest.mark.parametrize("sent_before_recovery", [False, True])
def test_hfr_474_startup_collapses_legacy_restart_notices_by_durable_turn(
    managers,
    tmp_path: Path,
    sent_before_recovery: bool,
) -> None:
    """Upgrade recovery uses exact Delivery ownership before notices can drain."""

    from core import failure_notices
    from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    manager, _restarted, engine, _engine_b, _starts = managers
    turn_id, _context_value = asyncio.run(_activate(manager))
    run_ids = [f"run-legacy-restart-{index}" for index in range(3)]
    now = "2026-08-01T00:00:00Z"
    with engine.begin() as conn:
        initial = delivery_store.initial_deliveries_for_turn(conn, turn_id)[0]
        assert initial["state"] == "accepted"
        initial_row = delivery_store.get_delivery(conn, str(initial["id"]))
        assert initial_row is not None
        delivery_ids = [str(initial["id"])]
        for index in range(1, len(run_ids)):
            delivery_id = delivery_store.new_delivery_id()
            values = dict(initial_row)
            values.update(
                id=delivery_id,
                priority="p1",
                dedupe_key=None,
                turn_role="steer",
                turn_position=index,
                submitted_at=now,
                updated_at=now,
                version=1,
            )
            conn.execute(message_deliveries.insert().values(**values))
            delivery_ids.append(delivery_id)
        for run_id, delivery_id in zip(run_ids, delivery_ids, strict=True):
            conn.execute(
                agent_runs.insert().values(
                    id=run_id,
                    definition_id=None,
                    run_type="agent_run",
                    status="running",
                    cancel_requested=0,
                    session_id="ses_fsm",
                    callback_status="pending",
                    delivery_id=delivery_id,
                    created_at=now,
                    started_at=now,
                    updated_at=now,
                    metadata_json="{}",
                )
            )

    db_path = Path(str(engine.url.database))
    run_store = SQLiteBackgroundTaskStore(db_path)
    request_store = TaskExecutionStore(root=tmp_path / "legacy-restart-requests")
    request_store._sqlite = run_store
    scheduled = ScheduledTaskService.__new__(ScheduledTaskService)
    scheduled.controller = manager.controller
    scheduled.request_store = request_store
    scheduled._drain_dirty = False
    manager.controller.scheduled_task_service = scheduled
    try:
        # Reproduce the old release: each failed Run independently receives a
        # restart notice although all rows retain the same exact durable Turn.
        for run_id in run_ids:
            assert run_store.settle_run_terminal(
                run_id,
                terminal_status="failed",
                error="service restarted",
                metadata={"interrupt_reason": SETTLED_BY_RESTARTED},
                updated_at=now,
            ) == "failed"
        sent_id = run_ids[0]
        if sent_before_recovery:
            run_store.update_owed_failure_notice(
                sent_id,
                state="sent",
                ack_evidence="receipt",
            )

        # Match an upgrade: the old process wrote the terminal Turn and per-Run
        # notices; the new process owns startup reconciliation.
        with engine.begin() as conn:
            terminal = manager._write_terminal_snapshot(
                conn,
                turn_id,
                outcome="failed",
                settled_by=SETTLED_BY_RESTARTED,
                evidence_kind="service_shutdown",
                evidence={"reason": "scheduled_service_shutdown"},
            )
        assert terminal["changed"] is True

        asyncio.run(manager.recover_durable_delivery_state(service_restart=True))

        notices = {
            run_id: run_store.owed_failure_notice(run_id) for run_id in run_ids
        }
        assert all(notice is not None for notice in notices.values())
        assert {
            notice["turn_id"] for notice in notices.values() if notice is not None
        } == {turn_id}
        assert {
            notice["turn_fallback_run_id"]
            for notice in notices.values()
            if notice is not None
        } == {sent_id}
        assert {
            tuple(notice["turn_participant_run_ids"])
            for notice in notices.values()
            if notice is not None
        } == {tuple(run_ids)}

        deliverable = [
            run_id
            for run_id, notice in notices.items()
            if notice is not None
            and notice["state"] == "pending"
            and failure_notices.decide(
                run_id=run_id,
                definition_id=None,
                notice=notice,
                streak_facts=None,
                earlier_unsettled=None,
            ).action
            == failure_notices.ACTION_DELIVER
        ]
        assert deliverable == ([] if sent_before_recovery else [sent_id])
        if sent_before_recovery:
            assert notices[sent_id]["state"] == "sent"
            assert all(
                notice["turn_notification_delivered"] is True
                for notice in notices.values()
                if notice is not None
            )
    finally:
        run_store.close()


def test_restored_opencode_generation_rebinds_turn_control_and_steer(
    managers,
) -> None:
    manager, _restarted, engine, _engine_b, _starts = managers
    old_native_id = "opencode:native-session:old-generation"
    new_native_id = "opencode:native-session:new-generation"
    with engine.begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == "ses_fsm")
            .values(agent_backend="opencode")
        )

    context = _context()
    context.platform_specific["agent_session_target"]["agent_backend"] = "opencode"
    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="active"),
            context=context,
        )
    )
    assert admitted.turn_id is not None
    turn_id = admitted.turn_id
    context.platform_specific["turn_token"] = turn_id
    context.platform_specific["agent_runtime_turn_token"] = f"runtime-{turn_id}"
    manager._active_identity = lambda _backend, _session, logical: (
        logical,
        old_native_id,
    )
    manager.on_native_start(
        context,
        backend="opencode",
        runtime_key="opencode:native-session",
        runtime_turn_id=f"runtime-{turn_id}",
    )

    steer_delivery_id = delivery_store.new_delivery_id()
    steer_attempt_id = delivery_store.new_attempt_id()
    with engine.begin() as conn:
        delivery_store.insert_delivery(
            conn,
            delivery_id=steer_delivery_id,
            session_id="ses_fsm",
            priority="p1",
            state="reserved",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses_fsm",
                platform="avibe",
                author="user",
                source="user",
                message_type="user",
                text="steer after restart",
            ),
            dispatch_text="steer after restart",
        )
        delivery = delivery_store.get_delivery(conn, steer_delivery_id)
        assert delivery is not None
        steering = delivery_store.open_steer_attempt(
            conn,
            steer_delivery_id,
            expected_version=int(delivery["version"]),
            turn_id=turn_id,
            attempt_id=steer_attempt_id,
            expected_native_turn_id=old_native_id,
        )
        assert steering is not None
        assert delivery_store.mark_attempt_unknown(
            conn,
            steer_delivery_id,
            expected_version=int(steering["version"]),
            receipt={"reason": "restart"},
        ) is not None
        turn = delivery_store.get_turn(conn, turn_id)
        assert turn is not None
        assert delivery_store.cas_turn(
            conn,
            turn_id,
            expected_version=int(turn["version"]),
            expected_states=("active",),
            values={
                "control_state": "pending",
                "control_mode": "stop_only",
                "control_attempt_id": delivery_store.new_attempt_id(),
                "control_expected_native_turn_id": old_native_id,
            },
        ) is not None

    reconciled_native_ids: list[str] = []

    async def reconcile(_backend, request):
        reconciled_native_ids.append(request.expected_native_turn_id)
        return steer_result(SteerOutcome.UNKNOWN, reason="still_unknown")

    manager._active_identity = lambda _backend, _session, logical: (
        logical,
        new_native_id,
    )
    manager._reconcile_steer_attempt = reconcile
    manager._run_pending_interrupt = AsyncMock(return_value={"state": "waiting_terminal"})

    asyncio.run(manager.recover_durable_delivery_state())

    with engine.connect() as conn:
        turn = delivery_store.get_turn(conn, turn_id)
        steer = delivery_store.get_delivery(conn, steer_delivery_id)
    assert turn is not None
    assert turn["native_turn_id"] == new_native_id
    assert turn["control_expected_native_turn_id"] == new_native_id
    assert steer is not None
    assert steer["current_expected_native_turn_id"] == new_native_id
    assert reconciled_native_ids == [new_native_id]
    manager._run_pending_interrupt.assert_awaited_once_with("ses_fsm", turn_id)


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
    terminal = other._terminalize_durable_turn(
        t1,
        "completed",
        settled_by="service_shutdown",
        evidence_kind="test_deferred_resume",
        resume_successors=False,
    )
    assert terminal["changed"] is True
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


def test_stale_send_now_does_not_mutate_the_deferred_queue(managers) -> None:
    manager, _other, engine, _engine_b, _starts = managers
    old_turn_id, _ = asyncio.run(_activate(manager, text="old turn"))
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(session_id="ses_fsm", priority="p3", content="deferred backlog"),
            context=_context(),
        )
    )
    with engine.connect() as conn:
        old_turn = delivery_store.get_turn(conn, old_turn_id)
    assert old_turn is not None
    terminal = manager._terminalize_durable_turn(
        old_turn_id,
        "completed",
        settled_by="service_shutdown",
        evidence_kind="test_deferred_resume",
        resume_successors=False,
    )
    assert terminal["changed"] is True
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
        current = delivery_store.active_turn(conn, "ses_fsm")
    assert current is not None and current["id"] == replacement_turn_id
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"


def test_send_now_keeps_attachment_head_for_the_next_turn(
    managers,
    tmp_path: Path,
) -> None:
    from storage import media_service

    manager, _other, engine, _engine_b, _starts = managers
    asyncio.run(_activate(manager, text="active turn"))
    attachment = tmp_path / "queued-input.txt"
    attachment.write_text("queued attachment", encoding="utf-8")
    with engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            kind="file",
            source="user_upload",
            local_path=str(attachment),
            file_name=attachment.name,
            content_type="text/plain",
        )
    queued = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p3",
                content="review this file",
                content_json={"attachments": [{"token": token}]},
            ),
            context=_context(),
        )
    )
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    promoted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content=None,
                expected_delivery_id=str(queued.delivery_id),
            ),
            context=_context(),
        )
    )

    assert promoted.state == "queued"
    assert promoted.reason == "attachments_wait_for_new_turn"
    manager._steer.assert_not_awaited()
    assert _row(engine, str(queued.delivery_id))["state"] == "queued"


def test_content_p1_with_attachment_queues_behind_an_active_turn(
    managers,
    tmp_path: Path,
) -> None:
    from storage import media_service

    manager, _other, engine, _engine_b, _starts = managers
    asyncio.run(_activate(manager, text="active turn"))
    attachment = tmp_path / "priority-input.txt"
    attachment.write_text("priority attachment", encoding="utf-8")
    with engine.begin() as conn:
        token = media_service.register(
            conn,
            scope_id=None,
            session_id="ses_fsm",
            kind="file",
            source="user_upload",
            local_path=str(attachment),
            file_name=attachment.name,
            content_type="text/plain",
        )
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))

    admitted = asyncio.run(
        manager.deliver(
            DeliveryRequest(
                session_id="ses_fsm",
                priority="p1",
                content="review this first",
                content_json={"attachments": [{"token": token}]},
            ),
            context=_context(),
        )
    )

    assert admitted.state == "queued"
    manager._steer.assert_not_awaited()
    row = _row(engine, str(admitted.delivery_id))
    assert row["priority"] == "p3"
    assert row["state"] == "queued"


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
async def test_agent_initiated_continuation_materializes_as_hidden_turn_input(
    managers,
) -> None:
    """HFR-461: backend continuation keeps a hidden lifecycle input."""

    manager, _other, engine, _engine_b, _starts = managers
    manager.controller.config.language = "zh"
    context = _context()

    assert manager.register_agent_initiated_turn(context) is True
    with engine.connect() as conn:
        row = messages_service.get_message(conn, str(context.message_id))
        transcript = messages_service.list_session_messages(
            conn,
            session_id="ses_fsm",
            types=messages_service.TRANSCRIPT_TYPES,
        )
    assert row is not None
    assert row["type"] == "agent_initiated"
    assert row["text"] == "Agent 主动发起的续接"
    assert transcript["messages"] == []

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
