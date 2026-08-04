from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, select, update

from core.runtime_ownership import (
    RuntimeOwnershipProvider,
    RuntimeResourceTarget,
    RuntimeSessionBinding,
    RuntimeTargetOwnershipSnapshot,
    SessionRuntimeOwnershipSnapshot,
    SessionRuntimeDisposition,
    wake_runtime_ownership,
)
from core.runtime_work import RuntimeWorkLane
from core.session_turns import SessionTurnManager
from modules.agents.codex.agent import CodexAgent
from modules.agents.codex.session import CodexSessionManager
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.service import AgentService
from storage import message_deliveries as delivery_store
from storage import workbench_sessions_service as workbench_sessions
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.models import (
    agents,
    agent_runs,
    agent_sessions,
    metadata,
    runtime_records,
)
from storage.session_activities import ACTIVE_PHASE, AWAITING_OUTPUT_PHASE


NOW = "2026-08-03T00:00:00+00:00"


def _engine(tmp_path: Path, name: str = "ownership.sqlite"):
    engine = create_sqlite_engine(tmp_path / name)
    metadata.create_all(engine)
    return engine


def _session(
    conn,
    session_id: str,
    *,
    anchor: str,
    workdir: str,
    backend: str = "codex",
    hold: str = "open",
) -> None:
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
            session_anchor=anchor,
            workdir=workdir,
            native_session_id="",
            title=None,
            status="active",
            visibility="foreground",
            pinned=0,
            agent_status="idle",
            queue_hold_state=hold,
            queue_hold_version=1,
            queue_held_at=NOW if hold == "held" else None,
            metadata_json="{}",
            created_at=NOW,
            updated_at=NOW,
            last_active_at=NOW,
        )
    )


@pytest.mark.anyio
async def test_hfr_131_open_head_wakes_survives_reclaim_and_claims_exact_head(
    tmp_path: Path,
) -> None:
    """HFR-131: waking an open head does not make the old runtime its owner."""
    engine = _engine(tmp_path, "hfr-131.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base-a", workdir="/work")
        _delivery(conn, "delivery-a", "ses-a")
        observed = delivery_store.get_delivery(conn, "delivery-a")

    agent, _manager, stopped, wakes = _codex_reclaimer(
        engine,
        [("ses-a", "base-a", "/work", "route:a")],
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("modules.agents.codex.agent.time.monotonic", lambda: 1000.0)
        assert await agent.evict_idle_transports(600) == 1

    assert stopped == ["stop"]
    assert any(RuntimeWorkLane.SESSION_DELIVERIES in lanes for lanes in wakes)
    with engine.connect() as conn:
        durable = delivery_store.get_delivery(conn, "delivery-a")
    assert durable is not None and durable["state"] == "queued"

    turn_manager = SessionTurnManager(SimpleNamespace())
    turn_manager._engine = engine
    turn_manager._start_persisted_turn = AsyncMock(return_value=True)
    assert await turn_manager.drain_delivery_queue(
        "ses-a",
        expected_head_id="delivery-a",
        expected_head_version=int(observed["version"]),
    )
    with engine.connect() as conn:
        claimed = delivery_store.get_delivery(conn, "delivery-a")
    assert claimed is not None and claimed["state"] == "claimed"
    turn_manager._start_persisted_turn.assert_awaited_once_with(claimed["turn_id"])
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_132_held_head_survives_reclaim_then_explicit_release_starts(
    tmp_path: Path,
) -> None:
    """HFR-132: a durable hold permits reclamation until explicit release."""
    engine = _engine(tmp_path, "hfr-132.sqlite")
    with engine.begin() as conn:
        _session(
            conn,
            "ses-held",
            anchor="base-held",
            workdir="/work",
            hold="held",
        )
        _delivery(conn, "delivery-held", "ses-held")

    agent, _manager, stopped, wakes = _codex_reclaimer(
        engine,
        [("ses-held", "base-held", "/work", "route:held")],
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("modules.agents.codex.agent.time.monotonic", lambda: 1000.0)
        assert await agent.evict_idle_transports(600) == 1
    assert stopped == ["stop"]
    assert wakes == []
    with engine.begin() as conn:
        durable = delivery_store.get_delivery(conn, "delivery-held")
        assert durable is not None and durable["state"] == "queued"
        assert delivery_store.set_queue_hold(conn, "ses-held", held=False)

    turn_manager = SessionTurnManager(SimpleNamespace())
    turn_manager._engine = engine
    turn_manager._start_persisted_turn = AsyncMock(return_value=True)
    assert await turn_manager.drain_delivery_queue(
        "ses-held",
        expected_head_id="delivery-held",
        expected_head_version=int(durable["version"]),
    )
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_137_new_pin_between_reclaimer_passes_wins_locked_check(
    tmp_path: Path,
) -> None:
    """HFR-137: the locked second pass preserves a newly admitted owner."""
    engine = _engine(tmp_path, "hfr-137.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base-a", workdir="/work")
        _delivery(conn, "delivery-a", "ses-a")

    agent, _manager, stopped, _wakes = _codex_reclaimer(
        engine,
        [("ses-a", "base-a", "/work", "route:a")],
    )
    lock = agent._transport_locks["/work"]
    await lock.acquire()
    first_snapshot = asyncio.Event()
    delegate = agent.controller.runtime_ownership
    calls = 0

    class _BarrierProvider:
        def snapshot(self, target):
            nonlocal calls
            calls += 1
            result = delegate.snapshot(target)
            if calls == 1:
                first_snapshot.set()
            return result

    agent.controller.runtime_ownership = _BarrierProvider()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("modules.agents.codex.agent.time.monotonic", lambda: 1000.0)
        eviction = asyncio.create_task(agent.evict_idle_transports(600))
        await first_snapshot.wait()
        with engine.begin() as conn:
            queued = delivery_store.get_delivery(conn, "delivery-a")
            delivery_store.claim_start_batch(
                conn,
                turn_id="turn-a",
                session_id="ses-a",
                backend="codex",
                deliveries=[queued],
                dispatch_text="delivery-a",
                attempt_id="attempt-a",
            )
        lock.release()
        assert await eviction == 0
    assert calls >= 2
    assert stopped == []
    engine.dispose()


def test_hfr_141_repeated_wakes_do_not_refresh_progress_clocks(tmp_path: Path) -> None:
    """HFR-141: queue recovery hints cannot manufacture runtime progress."""
    engine = _engine(tmp_path, "hfr-141.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base-a", workdir="/work")
        _delivery(conn, "delivery-a", "ses-a")
    agent, _manager, _stopped, wakes = _codex_reclaimer(
        engine,
        [("ses-a", "base-a", "/work", "route:a")],
    )
    agent._session_last_activity = {"base-a": 17.0}
    snapshot = agent._runtime_ownership_snapshot_for_cwd("/work")
    wake_runtime_ownership(agent.controller, snapshot)
    wake_runtime_ownership(agent.controller, snapshot)
    assert agent._session_last_activity == {"base-a": 17.0}
    assert len(wakes) == 3
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_146_shared_codex_target_and_claude_mapping_use_exact_bindings(
    tmp_path: Path,
) -> None:
    """HFR-146: shared Codex and composite Claude keys use exact bindings."""
    engine = _engine(tmp_path, "hfr-146.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-active", anchor="base-active", workdir="/work")
        _delivery(conn, "delivery-active", "ses-active")
        active_delivery = delivery_store.get_delivery(conn, "delivery-active")
        delivery_store.claim_start_batch(
            conn,
            turn_id="turn-active",
            session_id="ses-active",
            backend="codex",
            deliveries=[active_delivery],
            dispatch_text="delivery-active",
            attempt_id="attempt-active",
        )
        _session(
            conn,
            "ses-held",
            anchor="base-held",
            workdir="/work",
            hold="held",
        )
        _delivery(conn, "delivery-held", "ses-held")

    agent, manager, stopped, _wakes = _codex_reclaimer(
        engine,
        [
            ("ses-active", "base-active", "/work", "route:active"),
            ("ses-held", "base-held", "/work", "route:held"),
        ],
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("modules.agents.codex.agent.time.monotonic", lambda: 1000.0)
        assert await agent.evict_idle_transports(600) == 0
    assert stopped == []
    assert "base-held" not in agent._session_last_activity

    claude_client = SimpleNamespace(
        _vibe_agent_session_id="ses-active",
        _vibe_runtime_base_session_id="base-active",
        _vibe_runtime_session_key="claude-resource-key",
        _vibe_runtime_workdir="/work",
        _vibe_runtime_fallback_session_key="route:active",
    )
    from core.handlers.session_handler import SessionHandler

    handler = object.__new__(SessionHandler)
    handler.claude_sessions = {"claude-resource-key": claude_client}
    target = handler._claude_runtime_ownership_target(
        "claude-resource-key",
        claude_client,
    )
    assert target is not None
    assert target.resource_key == "claude-resource-key"
    assert target.bindings[0].session_id == "ses-active"
    assert target.bindings[0].session_anchor == "base-active"
    assert target.bindings[0].workdir == "/work"
    manager.set_agent_session_id("base-active", None)
    assert agent._runtime_ownership_target_for_cwd("/work") is None
    del claude_client._vibe_agent_session_id
    assert (
        handler._claude_runtime_ownership_target(
            "claude-resource-key",
            claude_client,
        )
        is None
    )
    engine.dispose()


def test_hfr_145_every_backend_invalidation_path_consumes_exact_ownership() -> None:
    """HFR-145: Claude, Codex, and OpenCode invalidation probes fail closed."""

    blocking = SimpleNamespace(blocks_reclamation=True)
    for backend in ("claude", "codex", "opencode"):
        probe = Mock(return_value=(blocking,))
        service = AgentService(SimpleNamespace())
        service.register(
            SimpleNamespace(
                name=backend,
                runtime_ownership_snapshots=probe,
                runtime_has_active_turns=Mock(return_value=False),
            )
        )
        assert asyncio.run(service.backend_runtime_active(backend))
        probe.assert_called_once_with()

    target_snapshots: list[RuntimeResourceTarget] = []
    nonblocking = SimpleNamespace(
        blocks_reclamation=False,
        needs_session_delivery_wake=False,
        needs_request_wake=False,
    )

    def snapshot(target: RuntimeResourceTarget):
        target_snapshots.append(target)
        return nonblocking

    controller = SimpleNamespace(
        runtime_ownership=SimpleNamespace(snapshot=snapshot),
        runtime_work_supervisor=SimpleNamespace(notify=Mock()),
    )
    opencode = object.__new__(OpenCodeAgent)
    opencode._client_manager = SimpleNamespace(
        _server_manager=SimpleNamespace(
            base_url="http://127.0.0.1:4096",
            runtime_has_active_turns=lambda: False,
        )
    )
    opencode._session_manager = SimpleNamespace(
        list_all=lambda: {"base-a": ("native-a", "/work", "route:a")},
        get_agent_session_id=lambda _base: "ses-a",
    )
    opencode._active_requests = {}
    opencode.runtime_turn_keys = lambda: {"base-a:/work"}
    opencode.controller = controller
    service = AgentService(controller)
    service.register(opencode)

    assert not asyncio.run(service.backend_runtime_active("opencode"))
    target = target_snapshots[0]
    assert target.resource_key == "http://127.0.0.1:4096"
    assert target.include_all_backend_sessions
    assert target.maps_all_backend_activities
    assert target.maps_all_backend_fallback_runs
    assert target.bindings[0].session_id == "ses-a"
    assert target.bindings[0].fallback_route_keys == ("route:a",)
    assert target.known_fallback_route_keys == ("route:a",)
    opencode._session_manager.get_agent_session_id = lambda _base: None
    assert opencode.runtime_ownership_snapshots() is None


def test_codex_ownership_probe_batches_off_the_event_loop() -> None:
    main_thread = threading.get_ident()
    worker_threads: list[int] = []
    batch_sizes: list[int] = []
    snapshots = (
        SimpleNamespace(blocks_reclamation=False),
        SimpleNamespace(blocks_reclamation=False),
    )

    def snapshot_many(targets):
        worker_threads.append(threading.get_ident())
        batch_sizes.append(len(targets))
        return snapshots

    agent = object.__new__(CodexAgent)
    agent._transports = {"/a": object(), "/b": object()}
    agent.controller = SimpleNamespace(
        runtime_ownership=SimpleNamespace(snapshot_many=snapshot_many),
    )
    agent._runtime_ownership_target_for_cwd = Mock(
        side_effect=lambda cwd: SimpleNamespace(resource_key=cwd)
    )

    result = asyncio.run(agent.runtime_ownership_snapshots())

    assert result == snapshots
    assert batch_sizes == [2]
    assert worker_threads and worker_threads[0] != main_thread


def test_agent_service_ownership_probe_runs_off_the_event_loop() -> None:
    main_thread = threading.get_ident()
    worker_threads: list[int] = []

    def probe():
        worker_threads.append(threading.get_ident())
        return (SimpleNamespace(blocks_reclamation=False),)

    service = AgentService(SimpleNamespace())
    service.register(
        SimpleNamespace(
            name="codex",
            runtime_ownership_snapshots=probe,
            runtime_has_active_turns=Mock(return_value=False),
        )
    )

    assert not asyncio.run(service.backend_runtime_active("codex"))
    assert worker_threads and worker_threads[0] != main_thread


def _delivery(conn, delivery_id: str, session_id: str, state: str = "queued") -> None:
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


def _run(
    conn,
    run_id: str,
    *,
    status: str,
    backend: str = "codex",
    session_id: str | None = None,
    legacy_session_key: str | None = None,
    run_type: str | None = "scheduled",
    delivery_id: str | None = None,
    pid: int | None = None,
) -> None:
    conn.execute(
        agent_runs.insert().values(
            id=run_id,
            definition_id=None,
            run_type=run_type,
            status=status,
            source_kind="cli",
            source_actor=None,
            parent_run_id=None,
            agent_name=backend,
            agent_id=None,
            agent_backend=backend,
            model=None,
            reasoning_effort=None,
            session_policy=None,
            session_id=session_id,
            legacy_session_key=legacy_session_key,
            post_to=None,
            deliver_key=None,
            prompt="run",
            message="run",
            message_payload_json="null",
            result_text=None,
            result_payload_json="null",
            message_ids_json="[]",
            delivery_id=delivery_id,
            callback_session_id=None,
            callback_status=None,
            callback_error=None,
            callback_run_id=None,
            callback_completed_at=None,
            cancel_requested=0,
            cancel_requested_at=None,
            pid=pid,
            exit_code=None,
            error=None,
            stdout=None,
            stderr=None,
            created_at=NOW,
            started_at=NOW if status in {"running", "processing"} else None,
            completed_at=None,
            updated_at=NOW,
            metadata_json="{}",
        )
    )


def _activity(
    conn,
    activity_id: str,
    *,
    runtime_key: str,
    phase: str,
    session_id: str | None,
    backend: str = "codex",
) -> None:
    activity = {
        "id": activity_id,
        "backend": backend,
        "runtime_key": runtime_key,
        "session_id": session_id,
    }
    conn.execute(
        runtime_records.insert().values(
            id=f"activity-row-{activity_id}",
            record_type="session_activity",
            record_key=activity_id,
            scope_id=None,
            session_anchor=session_id,
            workdir=None,
            payload_json=json.dumps(
                {"version": 1, "phase": phase, "activity": activity}
            ),
            expires_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _target(
    *,
    session_id: str = "ses-a",
    anchor: str = "base",
    workdir: str = "/work",
    backend: str = "codex",
    runtime_key: str | None = None,
    route_key: str = "route:base",
    known_route_keys: tuple[str, ...] | None = None,
    durable_session_workdir: str | None = None,
) -> RuntimeResourceTarget:
    activity_key = runtime_key or f"{anchor}:{workdir}"
    return RuntimeResourceTarget(
        backend=backend,
        resource_key=workdir,
        bindings=(
            RuntimeSessionBinding(
                session_id=session_id,
                session_anchor=anchor,
                workdir=workdir,
                activity_runtime_keys=(activity_key,),
                fallback_route_keys=(route_key,),
            ),
        ),
        known_activity_runtime_keys=(activity_key,),
        known_fallback_route_keys=known_route_keys or (route_key,),
        durable_session_workdir=durable_session_workdir,
    )


def test_snapshot_many_reads_backend_owner_tables_once(tmp_path: Path) -> None:
    engine = _engine(tmp_path, "ownership-batch.sqlite")
    with engine.begin() as conn:
        _session(
            conn,
            "ses-a",
            anchor="base-a",
            workdir="/work/a",
            backend="claude",
        )
        _session(
            conn,
            "ses-b",
            anchor="base-b",
            workdir="/work/b",
            backend="claude",
        )

    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        snapshots = RuntimeOwnershipProvider(engine).snapshot_many(
            (
                _target(
                    session_id="ses-a",
                    anchor="base-a",
                    workdir="/work/a",
                    backend="claude",
                    runtime_key="runtime-a",
                    route_key="route:a",
                ),
                _target(
                    session_id="ses-b",
                    anchor="base-b",
                    workdir="/work/b",
                    backend="claude",
                    runtime_key="runtime-b",
                    route_key="route:b",
                ),
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert tuple(snapshot.resource_key for snapshot in snapshots) == (
        "/work/a",
        "/work/b",
    )
    assert all(
        snapshot.disposition is SessionRuntimeDisposition.RECLAIMABLE
        for snapshot in snapshots
    )
    assert sum("FROM runtime_records" in statement for statement in statements) == 1
    assert sum("FROM agent_runs" in statement for statement in statements) == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    (
        (
            RuntimeTargetOwnershipSnapshot(
                backend="codex",
                resource_key="/work",
                activity_runtime_keys=(),
                sessions=(),
                sessionless_active_activity_ids=(),
                sessionless_fallback_run_ids=(),
                disposition=SessionRuntimeDisposition.UNKNOWN,
            ),
            True,
        ),
        (
            RuntimeTargetOwnershipSnapshot(
                backend="codex",
                resource_key="/work",
                activity_runtime_keys=(),
                sessions=(),
                sessionless_active_activity_ids=("activity-a",),
                sessionless_fallback_run_ids=(),
                disposition=SessionRuntimeDisposition.ACTIVE,
            ),
            True,
        ),
        (
            RuntimeTargetOwnershipSnapshot(
                backend="codex",
                resource_key="/work",
                activity_runtime_keys=(),
                sessions=(),
                sessionless_active_activity_ids=(),
                sessionless_fallback_run_ids=("run-a",),
                disposition=SessionRuntimeDisposition.ACTIVE,
                reasons=("run:run-a:active",),
            ),
            True,
        ),
        (
            RuntimeTargetOwnershipSnapshot(
                backend="codex",
                resource_key="/work",
                activity_runtime_keys=(),
                sessions=(
                    SessionRuntimeOwnershipSnapshot(
                        session_id="ses-a",
                        disposition=SessionRuntimeDisposition.ACTIVE,
                        delivery_ids=("delivery-a",),
                        turn_ids=("turn-a",),
                        active_activity_ids=(),
                        fallback_run_ids=(),
                        queue_hold_state="open",
                        reasons=("delivery:claimed", "turn:starting"),
                    ),
                ),
                sessionless_active_activity_ids=(),
                sessionless_fallback_run_ids=(),
                disposition=SessionRuntimeDisposition.ACTIVE,
            ),
            False,
        ),
        (
            RuntimeTargetOwnershipSnapshot(
                backend="codex",
                resource_key="/work",
                activity_runtime_keys=(),
                sessions=(
                    SessionRuntimeOwnershipSnapshot(
                        session_id="ses-a",
                        disposition=SessionRuntimeDisposition.ACTIVE,
                        delivery_ids=("delivery-a",),
                        turn_ids=("turn-a",),
                        active_activity_ids=(),
                        fallback_run_ids=(),
                        queue_hold_state="open",
                        reasons=("delivery:claimed", "turn:active"),
                    ),
                ),
                sessionless_active_activity_ids=(),
                sessionless_fallback_run_ids=(),
                disposition=SessionRuntimeDisposition.ACTIVE,
            ),
            True,
        ),
    ),
)
def test_transport_replacement_gate_tracks_only_durable_native_effect_owners(
    snapshot: RuntimeTargetOwnershipSnapshot,
    expected: bool,
) -> None:
    assert snapshot.blocks_transport_replacement is expected


def _codex_reclaimer(engine, bindings: list[tuple[str, str, str, str]]):
    stopped: list[str] = []
    wakes: list[tuple[RuntimeWorkLane, ...]] = []

    async def stop_transport() -> None:
        stopped.append("stop")

    manager = CodexSessionManager()
    for agent_session_id, base_session_id, cwd, session_key in bindings:
        manager.set_cwd(base_session_id, cwd)
        manager.set_session_key(base_session_id, session_key)
        manager.set_agent_session_id(base_session_id, agent_session_id)
    agent = object.__new__(CodexAgent)
    agent._transports = {"/work": SimpleNamespace(stop=stop_transport)}
    agent._transport_last_activity = {"/work": 0.0}
    agent._session_last_activity = {}
    agent._transport_locks = {"/work": asyncio.Lock()}
    agent._transport_cwd_inodes = {}
    agent._session_locks = {}
    agent._session_mgr = manager
    agent._turn_registry = SimpleNamespace(
        get_active_turn=lambda _base: None,
        has_pending_turn_start=lambda _base: False,
        clear_session=lambda _base: None,
    )
    agent.controller = SimpleNamespace(
        runtime_ownership=RuntimeOwnershipProvider(engine),
        runtime_work_supervisor=SimpleNamespace(
            notify=lambda *lanes: wakes.append(tuple(lanes))
        ),
        model_hub_runtime=None,
    )
    return agent, manager, stopped, wakes


def test_hfr_130_hfr_138_snapshot_prevents_torn_run_to_delivery_handoff(
    tmp_path: Path,
) -> None:
    """HFR-130/HFR-138: one explicit snapshot cannot mix handoff generations."""
    engine_a = _engine(tmp_path)
    engine_b = create_sqlite_engine(tmp_path / "ownership.sqlite")
    with engine_a.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _run(conn, "run-a", status="queued", session_id="ses-a")

    first_read = threading.Event()
    writer_done = threading.Event()

    def after_first_read() -> None:
        first_read.set()
        assert writer_done.wait(2)

    provider = RuntimeOwnershipProvider(engine_a, after_first_read=after_first_read)
    result: list[object] = []

    def read_snapshot() -> None:
        result.append(provider.snapshot(_target()))

    reader = threading.Thread(target=read_snapshot)
    reader.start()
    assert first_read.wait(2)
    with engine_b.begin() as conn:
        _delivery(conn, "delivery-a", "ses-a", "reserved")
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == "run-a")
            .values(delivery_id="delivery-a")
        )
    writer_done.set()
    reader.join(2)

    snapshot = result[0]
    assert snapshot.disposition is SessionRuntimeDisposition.RUNNABLE
    assert snapshot.sessions[0].fallback_run_ids == ("run-a",)
    assert snapshot.sessions[0].delivery_ids == ()
    engine_a.dispose()
    engine_b.dispose()


def test_hfr_138_exact_delivery_representation_supersedes_fallback_run(
    tmp_path: Path,
) -> None:
    """HFR-138: one exact Delivery representation replaces bare-Run ownership."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _delivery(conn, "delivery-a", "ses-a")
        _run(
            conn,
            "run-a",
            status="running",
            session_id="ses-a",
            delivery_id="delivery-a",
            pid=123,
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.RUNNABLE
    assert snapshot.sessions[0].delivery_ids == ("delivery-a",)
    assert snapshot.sessions[0].fallback_run_ids == ()
    engine.dispose()


@pytest.mark.parametrize("turn_state", ["starting", "active"])
def test_real_archived_session_live_turn_still_owns_runtime(
    tmp_path: Path,
    turn_state: str,
) -> None:
    """An archived Session cannot hide a still-unsettled native Turn owner."""

    engine = _engine(tmp_path, f"archived-turn-{turn_state}.sqlite")
    with engine.begin() as conn:
        _session(
            conn,
            "ses-archived",
            anchor="base",
            workdir="/work",
        )
        _delivery(conn, "delivery-a", "ses-archived")
        claimed = delivery_store.claim_start_batch(
            conn,
            turn_id="turn-a",
            session_id="ses-archived",
            backend="codex",
            deliveries=[delivery_store.get_delivery(conn, "delivery-a")],
            dispatch_text="delivery-a",
        )
        if turn_state == "active":
            turn = claimed["turn"]
            assert delivery_store.bind_native_start(
                conn,
                "turn-a",
                expected_version=int(turn["version"]),
                runtime_key="base:/work",
                runtime_turn_id="runtime-turn-a",
                native_turn_id="native-turn-a",
            )
        workbench_sessions.archive_session(conn, "ses-archived")
        archived = conn.execute(
            select(agent_sessions).where(agent_sessions.c.id == "ses-archived")
        ).mappings().one()
        assert archived["status"] == "archived"
        assert archived["session_anchor"] == "archived:ses-archived"

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(session_id="ses-archived")
    )
    assert snapshot.disposition is SessionRuntimeDisposition.ACTIVE
    assert snapshot.sessions[0].turn_ids == ("turn-a",)
    engine.dispose()


def test_real_archived_session_unresolved_delivery_still_owns_runtime(
    tmp_path: Path,
) -> None:
    """Archival changes admission visibility, not unresolved Delivery ownership."""

    engine = _engine(tmp_path, "archived-delivery.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-archived", anchor="base", workdir="/work")
        delivery_store.insert_delivery(
            conn,
            delivery_id="delivery-initial",
            session_id="ses-archived",
            priority="p3",
            state="queued",
            snapshot=delivery_store.message_snapshot(
                scope_id=None,
                session_id="ses-archived",
                platform="avibe",
                author="user",
                source="user",
                text="initial",
            ),
            dispatch_text="initial",
        )
        claimed = delivery_store.claim_start_batch(
            conn,
            turn_id="turn-a",
            session_id="ses-archived",
            backend="codex",
            deliveries=[delivery_store.get_delivery(conn, "delivery-initial")],
            dispatch_text="initial",
        )
        turn = claimed["turn"]
        active_turn = delivery_store.bind_native_start(
            conn,
            "turn-a",
            expected_version=int(turn["version"]),
            runtime_key="base:/work",
            runtime_turn_id="runtime-turn-a",
            native_turn_id="native-turn-a",
        )
        assert active_turn is not None
        assert delivery_store.materialize_start_acceptance(
            conn,
            turn_id="turn-a",
            evidence={"kind": "test"},
        )
        _delivery(conn, "delivery-steer", "ses-archived")
        steer = delivery_store.get_delivery(conn, "delivery-steer")
        assert delivery_store.open_steer_attempt(
            conn,
            "delivery-steer",
            expected_version=int(steer["version"]),
            turn_id="turn-a",
            attempt_id="steer-a",
            expected_native_turn_id="native-turn-a",
        )
        workbench_sessions.archive_session(conn, "ses-archived")
        assert delivery_store.terminalize_turn(
            conn,
            "turn-a",
            outcome="completed",
            settled_by="test",
            evidence_kind="test",
        )["changed"]

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(session_id="ses-archived")
    )
    assert snapshot.disposition is SessionRuntimeDisposition.TRANSITIONING
    assert snapshot.sessions[0].delivery_ids == ("delivery-steer",)
    assert snapshot.sessions[0].turn_ids == ()
    engine.dispose()


@pytest.mark.parametrize(
    ("pid", "expected"),
    [
        (None, SessionRuntimeDisposition.TRANSITIONING),
        (123, SessionRuntimeDisposition.ACTIVE),
    ],
)
def test_real_archived_session_running_fallback_run_still_owns_runtime(
    tmp_path: Path,
    pid: int | None,
    expected: SessionRuntimeDisposition,
) -> None:
    """Fallback Run ownership survives Session archival on both PID sides."""

    engine = _engine(tmp_path, f"archived-run-{pid}.sqlite")
    with engine.begin() as conn:
        _session(
            conn,
            "ses-archived",
            anchor="base",
            workdir="/work",
        )
        _run(
            conn,
            "run-a",
            status="running",
            session_id="ses-archived",
            pid=pid,
        )
        workbench_sessions.archive_session(conn, "ses-archived")

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(session_id="ses-archived")
    )
    assert snapshot.disposition is expected
    assert snapshot.sessions[0].fallback_run_ids == ("run-a",)
    engine.dispose()


def test_real_archived_session_without_unresolved_owner_is_reclaimable(
    tmp_path: Path,
) -> None:
    """Archival alone is not ownership and terminal history never pins."""

    engine = _engine(tmp_path, "archived-terminal-history.sqlite")
    with engine.begin() as conn:
        _session(
            conn,
            "ses-archived",
            anchor="base",
            workdir="/work",
        )
        _run(
            conn,
            "run-terminal",
            status="completed",
            session_id="ses-archived",
        )
        _delivery(conn, "delivery-terminal", "ses-archived", "retired")
        workbench_sessions.archive_session(conn, "ses-archived")

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(session_id="ses-archived")
    )
    assert snapshot.disposition is SessionRuntimeDisposition.RECLAIMABLE
    assert snapshot.sessions[0].fallback_run_ids == ()
    assert snapshot.sessions[0].delivery_ids == ()
    engine.dispose()


def test_unrelated_real_archived_session_does_not_pin_target(
    tmp_path: Path,
) -> None:
    """Archived provenance maps only its exact pre-archive resource binding."""

    engine = _engine(tmp_path, "unrelated-archived-owner.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-target", anchor="base", workdir="/work")
        _session(conn, "ses-other", anchor="other", workdir="/work")
        _delivery(conn, "delivery-other", "ses-other")
        delivery_store.claim_start_batch(
            conn,
            turn_id="turn-other",
            session_id="ses-other",
            backend="codex",
            deliveries=[delivery_store.get_delivery(conn, "delivery-other")],
            dispatch_text="other",
        )
        workbench_sessions.archive_session(conn, "ses-other")

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(session_id="ses-target")
    )
    assert snapshot.disposition is SessionRuntimeDisposition.RECLAIMABLE
    assert [item.session_id for item in snapshot.sessions] == ["ses-target"]
    engine.dispose()


def test_reused_anchor_does_not_transfer_archived_runtime_ownership(
    tmp_path: Path,
) -> None:
    """A replacement Session cannot inherit an archived row's native owner."""

    engine = _engine(tmp_path, "reused-archived-anchor.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-old", anchor="base", workdir="/work")
        _delivery(conn, "delivery-old", "ses-old")
        delivery_store.claim_start_batch(
            conn,
            turn_id="turn-old",
            session_id="ses-old",
            backend="codex",
            deliveries=[delivery_store.get_delivery(conn, "delivery-old")],
            dispatch_text="old",
        )
        workbench_sessions.archive_session(conn, "ses-old")
        _session(conn, "ses-new", anchor="base", workdir="/work")

    provider = RuntimeOwnershipProvider(engine)
    old_snapshot = provider.snapshot(_target(session_id="ses-old"))
    new_snapshot = provider.snapshot(_target(session_id="ses-new"))

    assert old_snapshot.disposition is SessionRuntimeDisposition.ACTIVE
    assert [item.session_id for item in old_snapshot.sessions] == ["ses-old"]
    assert new_snapshot.disposition is SessionRuntimeDisposition.RECLAIMABLE
    assert [item.session_id for item in new_snapshot.sessions] == ["ses-new"]
    engine.dispose()


@pytest.mark.parametrize(
    ("delivery_state", "turn_state", "expected"),
    [
        ("reserved", None, SessionRuntimeDisposition.TRANSITIONING),
        ("claimed", "starting", SessionRuntimeDisposition.ACTIVE),
        ("claimed", "active", SessionRuntimeDisposition.ACTIVE),
        ("interrupt_waiting", "waiting", SessionRuntimeDisposition.TRANSITIONING),
        ("retired", "terminal", SessionRuntimeDisposition.RECLAIMABLE),
    ],
)
def test_hfr_133_hfr_134_delivery_and_turn_contract_blocks_only_live_owners(
    tmp_path: Path,
    delivery_state: str,
    turn_state: str | None,
    expected: SessionRuntimeDisposition,
) -> None:
    """HFR-133/HFR-134: fences and live Turns block; terminal history does not."""
    engine = _engine(tmp_path, f"{delivery_state}-{turn_state}.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        initial_state = (
            "queued"
            if delivery_state in {"claimed", "interrupt_waiting"}
            else delivery_state
        )
        _delivery(conn, "delivery-a", "ses-a", initial_state)
        if turn_state:
            turn_id = "turn-delivery-a"
            delivery_store.insert_turn(
                conn,
                turn_id=turn_id,
                session_id="ses-a",
                initial_delivery_id="delivery-a",
                state="active" if turn_state == "terminal" else turn_state,
                backend="codex",
                dispatch_text="delivery-a",
            )
            if delivery_state in {"claimed", "interrupt_waiting"}:
                conn.execute(
                    update(delivery_store.message_deliveries)
                    .where(delivery_store.message_deliveries.c.id == "delivery-a")
                    .values(
                        state=delivery_state,
                        turn_id=turn_id,
                        turn_role="initial",
                        turn_position=0,
                    )
                )
            if turn_state == "terminal":
                delivery_store.terminalize_turn(
                    conn,
                    turn_id,
                    outcome="completed",
                    settled_by="test",
                    evidence_kind="test",
                )
    assert RuntimeOwnershipProvider(engine).snapshot(_target()).disposition is expected
    engine.dispose()


@pytest.mark.parametrize("mismatch", ["missing_delivery_half", "wrong_position"])
def test_hfr_134_waiting_successor_mismatch_fails_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    """HFR-134: a waiting/interrupt half is ownership-unknown unless exact."""

    engine = _engine(tmp_path, f"waiting-mismatch-{mismatch}.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _delivery(conn, "delivery-a", "ses-a")
        delivery_store.insert_turn(
            conn,
            turn_id="turn-a",
            session_id="ses-a",
            initial_delivery_id="delivery-a",
            state="waiting",
            backend="codex",
        )
        if mismatch == "wrong_position":
            conn.execute(
                update(delivery_store.message_deliveries)
                .where(delivery_store.message_deliveries.c.id == "delivery-a")
                .values(
                    state="interrupt_waiting",
                    turn_id="turn-a",
                    turn_role="initial",
                    turn_position=1,
                )
            )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_135_hfr_147_only_active_exact_mapped_activity_pins(
    tmp_path: Path,
) -> None:
    """HFR-135/HFR-147: only exact active Activity ownership pins a target."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _activity(
            conn,
            "active-sessionless",
            runtime_key="base:/work",
            phase=ACTIVE_PHASE,
            session_id=None,
        )
        _activity(
            conn,
            "awaiting",
            runtime_key="base:/work",
            phase=AWAITING_OUTPUT_PHASE,
            session_id="ses-a",
        )
        _activity(
            conn,
            "terminal",
            runtime_key="base:/work",
            phase="terminal",
            session_id="ses-a",
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.ACTIVE
    assert snapshot.sessionless_active_activity_ids == ("active-sessionless",)

    unmapped = RuntimeOwnershipProvider(engine).snapshot(
        _target(runtime_key="different-runtime-key")
    )
    assert unmapped.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_136_watch_runtime_never_pins_but_execution_run_does(tmp_path: Path) -> None:
    """HFR-136: bookkeeping heartbeats do not own execution resources."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _run(
            conn,
            "watch-heartbeat",
            status="running",
            session_id="ses-a",
            run_type="watch_runtime",
            pid=123,
        )
    provider = RuntimeOwnershipProvider(engine)
    assert provider.snapshot(_target()).disposition is SessionRuntimeDisposition.RECLAIMABLE

    with engine.begin() as conn:
        _run(
            conn,
            "watch-execution",
            status="running",
            session_id="ses-a",
            run_type="watch",
            pid=456,
        )
    assert provider.snapshot(_target()).disposition is SessionRuntimeDisposition.ACTIVE
    engine.dispose()


@pytest.mark.parametrize(
    ("status", "pid", "expected"),
    [
        ("queued", None, SessionRuntimeDisposition.RUNNABLE),
        ("processing", None, SessionRuntimeDisposition.TRANSITIONING),
        ("running", 123, SessionRuntimeDisposition.ACTIVE),
    ],
)
def test_hfr_148_hfr_150_fallback_run_uses_pid_boundary_and_exact_route(
    tmp_path: Path,
    status: str,
    pid: int | None,
    expected: SessionRuntimeDisposition,
) -> None:
    """HFR-148/HFR-150: fallback ownership uses PID state and exact routes."""
    engine = _engine(tmp_path, f"fallback-{status}-{pid}.sqlite")
    with engine.begin() as conn:
        _run(
            conn,
            "run-a",
            status=status,
            session_id=None,
            legacy_session_key="route:base",
            pid=pid,
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is expected
    assert snapshot.sessionless_fallback_run_ids == ("run-a",)

    unknown = RuntimeOwnershipProvider(engine).snapshot(
        _target(route_key="route:other")
    )
    assert unknown.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_150_enqueue_captures_backend_before_session_and_agent_switch(
    tmp_path: Path,
) -> None:
    """HFR-150: a Run keeps its enqueue-time backend and exact colon route."""

    db_path = tmp_path / "backend-capture.sqlite"
    engine = _engine(tmp_path, db_path.name)
    route_key = "slack::channel::C1::thread::1712.9"
    with engine.begin() as conn:
        conn.execute(
            agents.insert().values(
                id="agent-a",
                name="reviewer",
                normalized_name="reviewer",
                description=None,
                backend="codex",
                model=None,
                reasoning_effort=None,
                system_prompt=None,
                enabled=1,
                source="user",
                source_ref=None,
                metadata_json="{}",
                archived_at=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        _session(conn, "ses-a", anchor="base", workdir="/work")
    store = SQLiteBackgroundTaskStore(db_path)
    try:
        store.enqueue_run(
            {
                "id": "run-a",
                "run_type": "agent_run",
                "status": "running",
                "agent_id": "agent-a",
                "agent_name": "reviewer",
                "session_id": "ses-a",
                "legacy_session_key": route_key,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        assert store.get_run("run-a")["agent_backend"] == "codex"
        with engine.begin() as conn:
            conn.execute(
                update(agent_sessions)
                .where(agent_sessions.c.id == "ses-a")
                .values(agent_backend="opencode")
            )
            conn.execute(
                update(agents)
                .where(agents.c.id == "agent-a")
                .values(backend="opencode")
            )

        old_target = _target(route_key=route_key)
        old_snapshot = RuntimeOwnershipProvider(engine).snapshot(old_target)
        assert old_snapshot.disposition is SessionRuntimeDisposition.TRANSITIONING
        assert old_snapshot.sessionless_fallback_run_ids == ("run-a",)

        current_target = _target(backend="opencode", route_key=route_key)
        assert (
            RuntimeOwnershipProvider(engine).snapshot(current_target).disposition
            is SessionRuntimeDisposition.RECLAIMABLE
        )
        prefix_target = _target(route_key="slack::channel::C1")
        assert (
            RuntimeOwnershipProvider(engine).snapshot(prefix_target).disposition
            is SessionRuntimeDisposition.UNKNOWN
        )
    finally:
        store.close()
        engine.dispose()


def test_hfr_150_legacy_blank_backend_uses_its_durable_session(tmp_path: Path) -> None:
    """HFR-150: a legacy blank backend cannot disappear from its Session target."""

    engine = _engine(tmp_path, "legacy-blank-backend.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _run(
            conn,
            "run-a",
            status="running",
            backend="reviewer",
            session_id="ses-a",
        )
        conn.execute(
            update(agent_runs)
            .where(agent_runs.c.id == "run-a")
            .values(agent_backend=None)
        )

    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.TRANSITIONING
    assert snapshot.sessions[0].fallback_run_ids == ("run-a",)
    engine.dispose()


def test_hfr_150_reused_route_keeps_archived_same_backend_run_visible(
    tmp_path: Path,
) -> None:
    """HFR-150: route reuse cannot hide an archived Session's live Run."""

    engine = _engine(tmp_path, "archived-route-reuse.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-old", anchor="archived:old", workdir="/work")
        _session(conn, "ses-new", anchor="base", workdir="/work")
        _run(
            conn,
            "run-old",
            status="running",
            session_id="ses-old",
            legacy_session_key="route:base",
            pid=123,
        )

    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(
            session_id="ses-new",
            durable_session_workdir="/work",
        )
    )

    assert snapshot.disposition is SessionRuntimeDisposition.ACTIVE
    old_snapshot = next(
        item for item in snapshot.sessions if item.session_id == "ses-old"
    )
    assert old_snapshot.fallback_run_ids == ("run-old",)
    assert old_snapshot.reasons == ("run:run-old:active",)
    engine.dispose()


@pytest.mark.anyio
async def test_hfr_150_reused_route_run_blocks_codex_cwd_reclamation(
    tmp_path: Path,
) -> None:
    """HFR-150: replacement bindings cannot reclaim an old live cwd owner."""

    engine = _engine(tmp_path, "archived-route-reclaim.sqlite")
    with engine.begin() as conn:
        _session(conn, "ses-old", anchor="archived:old", workdir="/work")
        _session(conn, "ses-new", anchor="base", workdir="/work")
        _run(
            conn,
            "run-old",
            status="running",
            session_id="ses-old",
            legacy_session_key="route:base",
            pid=123,
        )

    agent, _manager, stopped, _wakes = _codex_reclaimer(
        engine,
        [("ses-new", "base", "/work", "route:base")],
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "modules.agents.codex.agent.time.monotonic",
            lambda: 1000.0,
        )
        assert await agent.evict_idle_transports(600) == 0

    assert stopped == []
    assert "/work" in agent._transports
    engine.dispose()


def test_hfr_148_unknown_execution_type_fails_closed(tmp_path: Path) -> None:
    """HFR-148: an unnamed execution-bearing Run type cannot be reclaimed."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _run(
            conn,
            "run-unknown",
            status="running",
            legacy_session_key="route:base",
            run_type="unknown_execution",
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_148_null_execution_type_fails_closed(tmp_path: Path) -> None:
    """HFR-148: a legacy NULL Run type cannot disappear from ownership."""

    database_path = tmp_path / "ownership.sqlite"
    engine = _engine(tmp_path)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agent_runs'"
        ).fetchone()[0]
        legacy_schema = schema.replace(
            "run_type VARCHAR NOT NULL",
            "run_type VARCHAR",
        )
        assert legacy_schema != schema
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'agent_runs'",
            (legacy_schema,),
        )
        schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.execute(f"PRAGMA schema_version = {schema_version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")

    engine = create_sqlite_engine(database_path)
    with engine.begin() as conn:
        _run(
            conn,
            "run-null-type",
            status="running",
            legacy_session_key="route:base",
            run_type=None,
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_148_unknown_run_status_fails_closed(tmp_path: Path) -> None:
    """HFR-148: an unknown nonterminal Run status cannot be reclaimed."""

    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _run(
            conn,
            "run-unknown-status",
            status="dispatching",
            legacy_session_key="route:base",
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.UNKNOWN
    engine.dispose()


def test_hfr_139_unrelated_session_does_not_pin_target(tmp_path: Path) -> None:
    """HFR-139: an unrelated durable Session cannot pin this resource."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        _session(conn, "ses-a", anchor="base", workdir="/work")
        _session(conn, "ses-b", anchor="other", workdir="/other")
        _run(conn, "run-b", status="running", session_id="ses-b", pid=123)
        _run(
            conn,
            "sessionless-run-b",
            status="running",
            legacy_session_key="route:other",
            pid=456,
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(
        _target(known_route_keys=("route:base", "route:other"))
    )
    assert snapshot.disposition is SessionRuntimeDisposition.RECLAIMABLE
    assert [item.session_id for item in snapshot.sessions] == ["ses-a"]
    engine.dispose()


def test_hfr_140_missing_binding_fails_open_but_provider_failure_fails_closed(
    tmp_path: Path,
) -> None:
    """HFR-140: proven absence fails open while missing safety data fails closed."""
    engine = _engine(tmp_path)
    assert (
        RuntimeOwnershipProvider(engine).snapshot(_target()).disposition
        is SessionRuntimeDisposition.RECLAIMABLE
    )
    engine.dispose()

    class _BrokenEngine:
        @staticmethod
        def connect():
            raise OSError("database unavailable")

    assert (
        RuntimeOwnershipProvider(_BrokenEngine()).snapshot(_target()).disposition
        is SessionRuntimeDisposition.UNKNOWN
    )


def test_hfr_140_partial_real_sqlite_schema_fails_closed(tmp_path: Path) -> None:
    """HFR-140: a partially migrated real database is never reclaimable."""

    engine = create_sqlite_engine(tmp_path / "partial.sqlite")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE agent_sessions "
            "(id TEXT PRIMARY KEY, agent_backend TEXT, status TEXT)"
        )
    snapshot = RuntimeOwnershipProvider(engine).snapshot(_target())
    assert snapshot.disposition is SessionRuntimeDisposition.UNKNOWN
    assert snapshot.reasons == ("provider_failure",)
    engine.dispose()
