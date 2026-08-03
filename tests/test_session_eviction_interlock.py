"""Idle eviction must not destroy a runtime a durable owner still holds.

Backend idle eviction is keyed by an in-memory runtime key (Claude's
``{base_session_id}:{workdir}``, a Codex transport's cwd) and gated by an
in-memory activity clock plus an ``active`` flag that only exists once a turn has
reached native start. Every durable owner shipped by #1134 predates that flag: a
``queued`` Delivery, a ``starting`` Turn, an execution-bearing Run that has not
reserved a Delivery yet. Evicting under one of those tears down the runtime the
owner is about to be dispatched into.

These tests drive the two real eviction entry points --
``SessionHandler.evict_idle_sessions`` and ``CodexAgent.evict_idle_transports``
-- against REAL durable rows in the isolated state database, so what is asserted
is the join between the two keyspaces, not a stub's opinion of it.

Scenarios: HFR-130 … HFR-149 (tests/scenarios/harness_failure_recovery/catalog.yaml).
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock

import pytest
from sqlalchemy import Select, text as sa_text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.handlers.session_handler as session_handler_module
import modules.agents.codex.agent as codex_agent_module
from core.handlers.session_handler import SessionHandler
from core.session_ownership import (
    UNRESOLVED_SNAPSHOT,
    DurableSessionOwnershipProvider,
)
from modules.agents.codex.agent import CodexAgent
from modules.im import MessageContext
from tests.test_claude_cli_path import (
    _Controller,
    _StubClaudeAgentOptions,
    _run_session,
)

NOW = "2026-07-01T00:00:00+00:00"
ANCHOR = "slack_C123"
BASE_SESSION_ID = "sesinterlock01"

# ``evict_idle_sessions(600)`` with the shipped multiplier/floor.
IDLE_TIMEOUT = 600.0
STUCK_THRESHOLD = 1800.0


# ---------------------------------------------------------------------------
# Durable seeding: the real schema, the real tables, the isolated home's DB.
# ---------------------------------------------------------------------------


def _state_db() -> Path:
    """The isolated home's state DB with the real migrated schema applied.

    The provider answers ``EMPTY_SNAPSHOT`` (positive proof: no durable table can
    hold an owner) when the schema is absent, so a test that forgets this would
    pass for the wrong reason -- eviction would proceed because nothing could
    pin, not because the interlock decided so.
    """

    from config import paths
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
    from storage.migrations import run_migrations

    path = paths.get_sqlite_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations(path)
    ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    return path


def _engine():
    from storage.db import get_cached_sqlite_engine

    return get_cached_sqlite_engine()


def _seed_session(
    session_id: str,
    *,
    anchor: str,
    workdir: str,
    backend: str = "claude",
) -> str:
    from storage.models import agent_sessions

    with _engine().begin() as conn:
        conn.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=None,
                agent_backend=backend,
                agent_name=backend,
                agent_variant="default",
                session_anchor=anchor,
                workdir=workdir,
                native_session_id=f"native-{session_id}",
                status="active",
                visibility="foreground",
                agent_status="idle",
                metadata_json="{}",
                created_at=NOW,
                updated_at=NOW,
                last_active_at=NOW,
            )
        )
    return session_id


def _seed_delivery(session_id: str, state: str, *, delivery_id: Optional[str] = None) -> str:
    """One Delivery row in ``state``, shaped the way the CHECK constraints demand.

    The per-state shape is not decoration: ``claimed`` requires turn membership,
    the steer states require a live attempt, and ``accepted`` requires a
    materialized Message. Seeding a row the schema would reject is how a test
    ends up asserting against a state production can never produce.
    """

    from storage.models import message_deliveries

    delivery_id = delivery_id or f"dlv-{state}-{session_id}"
    values: dict[str, Any] = dict(
        id=delivery_id,
        session_id=session_id,
        priority="p1",
        state=state,
        snapshot_json="{}",
        snapshot_sha256="0" * 64,
        dispatch_text="hello",
        dispatch_sha256="1" * 64,
        submitted_at=NOW,
        updated_at=NOW,
    )

    if state in ("claimed", "interrupt_waiting", "accepted"):
        values.update(
            turn_id=_seed_settled_turn(session_id, f"turn-for-{delivery_id}"),
            turn_role="initial",
            turn_position=0,
        )
    if state in ("pending_steer", "steering", "reconciling_steer"):
        values.update(
            current_attempt_id=f"att-{delivery_id}",
            current_attempt_kind="steer",
            current_target_turn_id=_seed_settled_turn(session_id, f"turn-for-{delivery_id}"),
            current_attempt_opened_at=NOW,
        )
    if state in ("steering", "reconciling_steer"):
        values["current_expected_native_turn_id"] = f"native-{delivery_id}"
    if state == "reconciling_steer":
        values["current_receipt_outcome"] = "unknown"
    if state == "accepted":
        values.update(
            message_id=_seed_message(session_id, f"msg-{delivery_id}"),
            materialized_at=NOW,
            snapshot_json=None,
            dispatch_text=None,
        )
    if state == "retired":
        values["retired_at"] = NOW

    with _engine().begin() as conn:
        conn.execute(message_deliveries.insert().values(**values))
    return delivery_id


def _seed_message(session_id: str, message_id: str) -> str:
    from storage.models import messages

    with _engine().begin() as conn:
        conn.execute(
            messages.insert().values(
                id=message_id,
                session_id=session_id,
                platform="slack",
                author="user",
                content_text="hello",
                content_json="{}",
                metadata_json="{}",
                type="user",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return message_id


def _seed_settled_turn(session_id: str, turn_id: str) -> str:
    """A terminal Turn to hang turn-membership references on.

    Terminal history contributes no pin of its own, so a Delivery whose shape
    requires a Turn reference still tests exactly the Delivery's own pin.
    """

    from storage.models import session_turns

    with _engine().begin() as conn:
        existing = conn.execute(
            sa_text("SELECT id FROM session_turns WHERE id = :id"), {"id": turn_id}
        ).scalar()
        if existing:
            return turn_id
        initial_delivery_id = f"dlv-initial-{turn_id}"
        conn.execute(
            sa_text(
                "INSERT INTO message_deliveries "
                "(id, session_id, priority, state, snapshot_json, snapshot_sha256, "
                " dispatch_text, dispatch_sha256, submitted_at, updated_at, retired_at) "
                "VALUES (:id, :session_id, 'p1', 'retired', '{}', :sha, 'hello', :sha, "
                " :now, :now, :now)"
            ),
            {"id": initial_delivery_id, "session_id": session_id, "sha": "0" * 64, "now": NOW},
        )
        conn.execute(
            session_turns.insert().values(
                id=turn_id,
                session_id=session_id,
                initial_delivery_id=initial_delivery_id,
                state="terminal",
                backend="claude",
                terminal_outcome="not_written",
                terminal_at=NOW,
                start_receipt_json="{}",
                terminal_evidence_json="{}",
                control_receipt_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return turn_id


def _seed_turn(
    session_id: str,
    state: str,
    *,
    runtime_key: Optional[str] = None,
    turn_id: Optional[str] = None,
    backend: str = "claude",
) -> str:
    """A Turn in ``state`` plus the terminal Delivery it was started from.

    ``initial_delivery_id`` carries a real foreign key, and the seeded parent is
    terminal so it contributes no pin of its own -- what is asserted is the
    Turn's pin. The start-shape columns follow ``ck_session_turns_start_shape``:
    a ``waiting`` Turn has no dispatch yet, ``starting`` has one with no receipt,
    ``active`` has an accepted receipt, ``terminal`` carries its outcome.
    """

    from storage.models import session_turns

    turn_id = turn_id or f"turn-{state}-{session_id}"
    initial_delivery_id = _seed_delivery(
        session_id, "retired", delivery_id=f"dlv-initial-{turn_id}"
    )
    values: dict[str, Any] = dict(
        id=turn_id,
        session_id=session_id,
        initial_delivery_id=initial_delivery_id,
        state=state,
        backend=backend,
        runtime_key=runtime_key,
        start_receipt_json="{}",
        terminal_evidence_json="{}",
        control_receipt_json="{}",
        created_at=NOW,
        updated_at=NOW,
    )
    if state != "waiting":
        values.update(
            start_attempt_id=f"start-{turn_id}",
            dispatch_text="hello",
            dispatch_sha256="1" * 64,
        )
    if state in ("active", "terminal"):
        values["start_receipt_outcome"] = "accepted"
    if state == "terminal":
        values.update(terminal_outcome="completed", terminal_at=NOW)

    with _engine().begin() as conn:
        conn.execute(session_turns.insert().values(**values))
    return turn_id


def _seed_run(
    session_id: str,
    *,
    run_type: str = "agent_run",
    status: str = "running",
    delivery_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    from storage.models import agent_runs

    run_id = run_id or f"run-{run_type}-{status}-{session_id}"
    with _engine().begin() as conn:
        conn.execute(
            agent_runs.insert().values(
                id=run_id,
                run_type=run_type,
                status=status,
                session_id=session_id,
                delivery_id=delivery_id,
                cancel_requested=0,
                created_at=NOW,
                updated_at=NOW,
                metadata_json="{}",
            )
        )
    return run_id


def _set_delivery_state(delivery_id: str, state: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            sa_text("UPDATE message_deliveries SET state = :state WHERE id = :id"),
            {"state": state, "id": delivery_id},
        )


def _drop_session_row(session_id: str) -> None:
    """Delete a session row while its Delivery still points at it.

    Foreign keys are ON for every pooled connection, so the only way to build a
    *positively* dangling binding -- the failure the provider must fail OPEN for
    -- is to suspend enforcement for this one write.
    """

    raw = _engine().raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,))
        raw.commit()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# Claude harness
# ---------------------------------------------------------------------------


def _claude_handler(monkeypatch, tmp_path: Path, *, now: float = 1000.0):
    """One live Claude runtime for ``slack_C123`` at ``tmp_path``, frozen clock."""

    captured: dict[str, Any] = {"disconnects": 0}

    class _StubClaudeSDKClient:
        def __init__(self, options):
            captured["options"] = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            captured["disconnects"] += 1

    monkeypatch.setattr(session_handler_module, "ClaudeAgentOptions", _StubClaudeAgentOptions)
    monkeypatch.setattr(session_handler_module, "ClaudeSDKClient", _StubClaudeSDKClient)
    monkeypatch.setattr(session_handler_module.time, "monotonic", lambda: now)

    controller = _Controller(tmp_path)
    handler = SessionHandler(controller)
    _run_session(handler, MessageContext(user_id="U123", channel_id="C123"))
    composite_key = f"{ANCHOR}:{tmp_path}"
    handler.session_last_activity[composite_key] = 0.0
    return handler, controller, composite_key, captured


def _add_second_runtime(handler, composite_key: str, captured: dict[str, Any]) -> None:
    """A second, unrelated Claude runtime sharing the workdir."""

    class _Client:
        async def disconnect(self) -> None:
            captured[composite_key] = captured.get(composite_key, 0) + 1

    handler.claude_sessions[composite_key] = _Client()
    handler.session_last_activity[composite_key] = 0.0


def _sweep(handler, idle_timeout: float = IDLE_TIMEOUT) -> int:
    return asyncio.run(handler.evict_idle_sessions(idle_timeout))


# ---------------------------------------------------------------------------
# HFR-130 … HFR-141 + HFR-147: the Claude interlock
# ---------------------------------------------------------------------------


def test_queued_delivery_pins_its_own_idle_claude_session(monkeypatch, tmp_path: Path) -> None:
    """HFR-130: durable queued input outranks the idle clock, for that session only.

    The baseline defect: ``queued`` is the state a Delivery sits in *before* any
    turn starts, so the ``active`` flag is unset and the runtime looks perfectly
    idle. Evicting it destroys the SDK client the queued row is about to be
    dispatched into. RED against master (both runtimes evicted, ``evicted == 2``).
    """

    _state_db()
    handler, controller, pinned_key, captured = _claude_handler(monkeypatch, tmp_path)
    unrelated_key = f"slack_C999:{tmp_path}"
    _add_second_runtime(handler, unrelated_key, captured)

    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_delivery(BASE_SESSION_ID, "queued")

    evicted = _sweep(handler)

    assert evicted == 1, "only the unpinned runtime may be evicted"
    assert pinned_key in controller.claude_sessions, "queued durable input must pin its runtime"
    assert captured["disconnects"] == 0
    assert unrelated_key not in controller.claude_sessions, (
        "a pin on one session must not immunize every idle runtime"
    )
    # Waiting is not activity: the pin must not have refreshed the clock, or a
    # stream of queued followers could keep a dead runtime alive forever.
    assert handler.session_last_activity[pinned_key] == 0.0


@pytest.mark.parametrize(
    "state, pins",
    [
        ("reserved", True),
        ("queued", True),
        ("claimed", True),
        ("pending_steer", True),
        ("steering", True),
        ("interrupt_waiting", True),
        ("reconciling_steer", True),
        ("accepted", False),
        ("retired", False),
    ],
)
def test_delivery_pin_follows_the_state_policy(
    monkeypatch, tmp_path: Path, state: str, pins: bool
) -> None:
    """HFR-131: the pin is read from ``DELIVERY_STATE_MATRIX``, not a hard-coded list.

    Every ``claimable`` / ``fence`` / ``turn_owned`` row still owns its input and
    pins; only ``terminal`` history does not. Asserted state by state so a future
    Delivery state cannot be added with the wrong eviction answer, and so
    accepted/retired history cannot make a session immortal.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_delivery(BASE_SESSION_ID, state)

    evicted = _sweep(handler)

    if pins:
        assert evicted == 0 and composite_key in controller.claude_sessions
        assert captured["disconnects"] == 0
    else:
        assert evicted == 1 and composite_key not in controller.claude_sessions
        assert captured["disconnects"] == 1


@pytest.mark.parametrize("state", ["waiting", "starting", "active"])
def test_nonterminal_turn_pins_through_anchor_and_workdir(
    monkeypatch, tmp_path: Path, state: str
) -> None:
    """HFR-132: a Turn with no ``runtime_key`` yet still pins its runtime.

    ``runtime_key`` is only bound at ``bind_native_start``, so a ``waiting`` or
    ``starting`` Turn carries NULL -- exactly the window where the in-memory
    ``active`` flag is also unset. The join therefore composes the same key from
    ``session_anchor`` + normalized ``workdir`` rather than requiring the
    authoritative column.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_turn(BASE_SESSION_ID, state)

    assert _sweep(handler) == 0
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0


def test_terminal_turn_history_does_not_pin(monkeypatch, tmp_path: Path) -> None:
    """HFR-141: a settled Turn is history and must not veto eviction."""

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_turn(BASE_SESSION_ID, "terminal")

    assert _sweep(handler) == 1
    assert composite_key not in controller.claude_sessions
    assert captured["disconnects"] == 1


def test_watch_runtime_heartbeat_run_does_not_pin(monkeypatch, tmp_path: Path) -> None:
    """HFR-133: a supervisor heartbeat is not execution.

    A ``watch_runtime`` Run stays ``running`` for the entire life of the waiter --
    hours or days. Counting it as an owner would make every watched session
    permanently unevictable, which is the opposite failure from the one this PR
    fixes, so the classification is explicit.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_run(BASE_SESSION_ID, run_type="watch_runtime", status="running")

    assert _sweep(handler) == 1, "a heartbeat Run must not pin a runtime"
    assert composite_key not in controller.claude_sessions
    assert captured["disconnects"] == 1


@pytest.mark.parametrize("run_type", ["agent_run", "scheduled", "watch", "some_future_run_type"])
def test_execution_bearing_and_unclassified_runs_pin(
    monkeypatch, tmp_path: Path, run_type: str
) -> None:
    """HFR-134: execution-bearing Runs pin, and an unknown run type fails closed.

    The bare-Run window is real: a Run is created, resolves its target session,
    and only then reserves a Delivery. Nothing durable but the Run itself points
    at the session in between. An unrecognized ``run_type`` takes the same
    answer -- a new run type may not silently lose its interlock.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_run(BASE_SESSION_ID, run_type=run_type, status="running")

    assert _sweep(handler) == 0
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0


def test_terminal_run_history_does_not_pin(monkeypatch, tmp_path: Path) -> None:
    """HFR-134: a completed Run owns nothing."""

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_run(BASE_SESSION_ID, run_type="agent_run", status="succeeded")

    assert _sweep(handler) == 1
    assert composite_key not in controller.claude_sessions


def test_work_admitted_between_the_two_passes_wins(monkeypatch, tmp_path: Path) -> None:
    """HFR-135: the losing race -- a pin that appears after pass 1 still holds.

    ``evict_idle_sessions`` decides in one pass and acts in another. Input
    admitted in that window is durable and unstarted, so the second pass must
    re-read the owner union instead of trusting the first pass's verdict.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))

    provider = DurableSessionOwnershipProvider()
    reads = {"count": 0}

    class _AdmitsBetweenPasses:
        def snapshot(self):
            reads["count"] += 1
            if reads["count"] == 2:
                _seed_delivery(BASE_SESSION_ID, "queued")
            return provider.snapshot()

    controller.session_ownership = _AdmitsBetweenPasses()

    evicted = _sweep(handler)

    assert reads["count"] == 2, "both passes must consult the owner union"
    assert evicted == 0, "work admitted between the passes must defeat the decided eviction"
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0


def test_ownership_admitted_during_an_earlier_teardown_vetoes_the_later_eviction(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-147: the acting pass re-resolves per candidate, not once per pass.

    HFR-135's re-read is necessary but not sufficient. The acting pass walks a
    LIST of candidates and every teardown awaits, so a Delivery committed while
    an earlier candidate is being cleaned up lands after a once-per-pass
    snapshot was taken. Queued work deliberately touches neither
    ``session_last_activity`` nor ``active_sessions`` -- the two things the
    per-candidate recheck already re-derives -- so nothing else in the loop can
    notice it, and the later runtime is torn down with input already accepted.
    """

    _state_db()
    handler, controller, first_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))

    second_anchor = "slack_C999"
    second_base = "sesinterlock02"
    _seed_session(second_base, anchor=second_anchor, workdir=str(tmp_path))
    second_key = f"{second_anchor}:{tmp_path}"
    _add_second_runtime(handler, second_key, captured)

    class _AdmitsDuringTeardown:
        """The first candidate's disconnect is when the follower is accepted."""

        async def disconnect(self) -> None:
            captured["disconnects"] += 1
            _seed_delivery(second_base, "queued", delivery_id="dlv-admitted-mid-teardown")

    handler.claude_sessions[first_key] = _AdmitsDuringTeardown()

    evicted = _sweep(handler)

    assert captured["disconnects"] == 1, "the first candidate must really have been torn down"
    with _engine().connect() as conn:
        admitted = conn.execute(
            sa_text("SELECT state FROM message_deliveries WHERE id = 'dlv-admitted-mid-teardown'")
        ).fetchone()
    assert admitted is not None and admitted[0] == "queued", "the race must really have happened"
    assert evicted == 1, "only the first candidate may be evicted"
    assert second_key in handler.claude_sessions
    assert captured.get(second_key, 0) == 0, "the newly owned runtime must survive"


def test_unresolved_ownership_skips_the_whole_cycle(monkeypatch, tmp_path: Path) -> None:
    """HFR-136: missing safety data is not evidence that eviction is safe.

    A provider-wide failure (an unreadable DB, a raising lookup) says nothing
    about who owns what, so no session is evicted -- including ones no owner
    would have pinned. The cost is one deferred sweep; the alternative is
    destroying a runtime mid-dispatch on the basis of a failed read.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))

    controller.session_ownership = SimpleNamespace(snapshot=lambda: UNRESOLVED_SNAPSHOT)
    assert _sweep(handler) == 0
    assert composite_key in controller.claude_sessions

    def _raise():
        raise sqlite3.OperationalError("database is locked")

    controller.session_ownership = SimpleNamespace(snapshot=_raise)
    assert _sweep(handler) == 0
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0


def test_a_dangling_binding_fails_open_for_itself_alone(monkeypatch, tmp_path: Path) -> None:
    """HFR-137: a deleted target cannot pin, and cannot poison the live pin.

    Fail-closed is right for a lookup that *could not read*; it is wrong for a
    binding whose session row is positively gone, which would otherwise veto
    eviction of an unrelated runtime forever. Both halves are asserted in one
    sweep: the dangling row's runtime is evicted, the live owner's is not.
    """

    _state_db()
    handler, controller, live_key, captured = _claude_handler(monkeypatch, tmp_path)
    dangling_key = f"slack_C999:{tmp_path}"
    _add_second_runtime(handler, dangling_key, captured)

    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_delivery(BASE_SESSION_ID, "queued")
    _seed_session("sesdangling01", anchor="slack_C999", workdir=str(tmp_path))
    _seed_delivery("sesdangling01", "queued", delivery_id="dlv-dangling")
    _drop_session_row("sesdangling01")

    evicted = _sweep(handler)

    assert evicted == 1
    assert live_key in controller.claude_sessions, "the live owner still pins"
    assert dangling_key not in controller.claude_sessions, (
        "a binding whose session row is gone must fail open for itself"
    )


def test_the_pin_is_bounded_by_the_stuck_active_threshold(monkeypatch, tmp_path: Path) -> None:
    """HFR-138: an owner that never settles cannot make a runtime immortal.

    The pin borrows the existing inactivity clock and stuck-active threshold
    rather than adding one, and a new owner never restarts that clock. Past the
    bound the runtime is torn down through the settling path (#1140), which
    retires the pending request and turn token, instead of the plain idle
    disconnect -- the durable owner is still unsettled at that point.
    """

    _state_db()
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_delivery(BASE_SESSION_ID, "claimed")

    # Just inside the bound: pinned, and the clock is untouched.
    handler, controller, composite_key, captured = _claude_handler(
        monkeypatch, tmp_path, now=STUCK_THRESHOLD - 1.0
    )
    assert _sweep(handler) == 0
    assert handler.session_last_activity[composite_key] == 0.0

    # Past it: force-evicted, through the settling teardown.
    handler, controller, composite_key, captured = _claude_handler(
        monkeypatch, tmp_path, now=STUCK_THRESHOLD + 1.0
    )
    settled: list[str] = []
    controller.agent_service = SimpleNamespace(
        agents={
            "claude": SimpleNamespace(
                force_cleanup_stuck_active_session=lambda key: _record(settled, key)
            )
        }
    )

    assert _sweep(handler) == 1
    assert settled == [composite_key], (
        "a spent pin must settle its unsettled owner, not just disconnect the client"
    )


async def _record_async(sink: list[str], key: str) -> None:
    sink.append(key)


def _record(sink: list[str], key: str):
    return _record_async(sink, key)


def test_a_parent_turn_pins_its_subagent_runtime(monkeypatch, tmp_path: Path) -> None:
    """HFR-139: a subagent runtime is projected from its parent's session.

    A subagent's base session id is ``{parent_anchor}:{agent_name}``, so its
    composite key contains two colons and splits on the LAST one. A live parent
    Turn owns that runtime too; evicting it kills the subagent mid-flight.
    """

    _state_db()
    handler, controller, _parent_key, captured = _claude_handler(monkeypatch, tmp_path)
    subagent_key = f"{ANCHOR}:reviewer:{tmp_path}"
    _add_second_runtime(handler, subagent_key, captured)

    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_turn(BASE_SESSION_ID, "active", runtime_key=f"{ANCHOR}:{tmp_path}")

    assert _sweep(handler) == 0
    assert subagent_key in controller.claude_sessions
    assert captured["disconnects"] == 0


def test_a_handoff_committed_mid_read_cannot_fall_between_two_reads(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-140: the union is ONE snapshot, so an ownership handoff is atomic to it.

    The dangerous interleaving is not a lost update, it is a lost OWNER: read
    Deliveries (only terminal history), then a commit lands that settles the
    running Turn and admits a follower, then read Turns (now terminal). Two
    independent reads see no owner at all and evict a session with queued input.
    One ``BEGIN DEFERRED`` snapshot sees the pre-handoff world consistently, so
    the Turn still pins -- and the next sweep sees the follower.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    turn_id = _seed_turn(BASE_SESSION_ID, "active")

    def _commit_handoff() -> None:
        """Settle the Turn and admit a follower, from another connection."""

        with _engine().begin() as conn:
            conn.execute(
                sa_text(
                    "UPDATE session_turns SET state = 'terminal', "
                    "terminal_outcome = 'completed', terminal_at = :now WHERE id = :id"
                ),
                {"id": turn_id, "now": NOW},
            )
        _seed_delivery(BASE_SESSION_ID, "queued", delivery_id="dlv-follower")

    controller.session_ownership = DurableSessionOwnershipProvider(
        engine_factory=lambda: _CommitBetweenReads(_engine(), _commit_handoff)
    )

    assert _sweep(handler) == 0, "the snapshot's Turn must still pin the runtime"
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0
    with _engine().connect() as conn:
        state = conn.execute(
            sa_text("SELECT state FROM session_turns WHERE id = :id"), {"id": turn_id}
        ).scalar()
    assert state == "terminal", "the competing handoff really did commit mid-read"


class _CommitBetweenReads:
    """Engine proxy that lands a competing commit after the Deliveries read."""

    def __init__(self, engine, on_delivery_read) -> None:
        self._engine = engine
        self._on_delivery_read = on_delivery_read

    def connect(self):
        return _ConnectionProxy(self._engine.connect(), self._on_delivery_read)


class _ConnectionProxy:
    def __init__(self, conn, on_delivery_read) -> None:
        self._conn = conn
        self._on_delivery_read = on_delivery_read
        self._fired = False

    def execute(self, statement, *args, **kwargs):
        result = self._conn.execute(statement, *args, **kwargs)
        if (
            not self._fired
            and isinstance(statement, Select)
            and "message_deliveries" in str(statement)
        ):
            self._fired = True
            # Materialize the read before the competing write, exactly as the
            # provider does: the snapshot is established, not merely requested.
            result = _Rows(list(result.mappings()))
            self._on_delivery_read()
        return result

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return self._rows


# ---------------------------------------------------------------------------
# HFR-142 … HFR-146: the Codex and OpenCode consumers
# ---------------------------------------------------------------------------


def _codex_agent(workdir: str, *, ownership=None):
    """A Codex agent with one live transport keyed by ``workdir``."""

    agent = object.__new__(CodexAgent)
    stopped: list[str] = []
    invalidated: list[str] = []
    cleared: list[str] = []

    async def stop_transport() -> None:
        stopped.append(workdir)

    agent._transports = {workdir: SimpleNamespace(stop=stop_transport)}
    agent._transport_last_activity = {workdir: 0.0}
    agent._transport_locks = {workdir: asyncio.Lock()}
    agent._session_mgr = SimpleNamespace(
        sessions_for_cwd=lambda cwd: ["codex-session-1"] if cwd == workdir else [],
        invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
    )
    agent._turn_registry = SimpleNamespace(
        get_active_turn=lambda base_session_id: None,
        clear_session=lambda base_session_id: cleared.append(base_session_id),
    )
    agent._session_locks = {"codex-session-1": asyncio.Lock()}
    agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
    agent.controller = SimpleNamespace(
        model_hub_runtime=SimpleNamespace(retire_process_scope=Mock()),
        session_ownership=ownership,
    )
    return agent, SimpleNamespace(stopped=stopped, invalidated=invalidated, cleared=cleared)


def _codex_sweep(agent, monkeypatch, *, now: float = 1000.0) -> int:
    monkeypatch.setattr(codex_agent_module.time, "monotonic", lambda: now)
    return asyncio.run(agent.evict_idle_transports(IDLE_TIMEOUT))


def test_codex_transport_is_pinned_by_a_durable_owner(monkeypatch, tmp_path: Path) -> None:
    """HFR-142: a Codex transport is a projection too, keyed by working directory.

    Same defect, different keyspace: the app-server process is shared per cwd and
    evicted on the same idle clock, with the same pre-``active`` window. RED
    against master (the transport is stopped while a queued Delivery waits).
    """

    _state_db()
    workdir = str(tmp_path)
    _seed_session("sescodex0001", anchor="slack_C777", workdir=workdir, backend="codex")
    _seed_delivery("sescodex0001", "queued")

    agent, calls = _codex_agent(workdir)

    assert _codex_sweep(agent, monkeypatch) == 0
    assert calls.stopped == [], "a durably-owned transport must stay up"
    assert workdir in agent._transports
    assert agent._transport_last_activity[workdir] == 0.0, "the pin must not touch the clock"


def test_codex_unresolved_ownership_skips_the_cycle(monkeypatch, tmp_path: Path) -> None:
    """HFR-143: the Codex sweep fails closed on an unresolved union, like Claude's."""

    _state_db()
    workdir = str(tmp_path)
    agent, calls = _codex_agent(
        workdir, ownership=SimpleNamespace(snapshot=lambda: UNRESOLVED_SNAPSHOT)
    )

    assert _codex_sweep(agent, monkeypatch) == 0
    assert calls.stopped == []
    assert workdir in agent._transports


def test_codex_ownership_admitted_inside_the_lock_vetoes_eviction(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-144: the Codex recheck inside the per-cwd lock re-reads the union.

    The transport lock serializes eviction against start-up, and the recheck it
    guards already re-derived activity and the active-turn flag from current
    state. Durable ownership belongs in that same recheck, or work admitted while
    the sweep waited for the lock loses its runtime.
    """

    _state_db()
    workdir = str(tmp_path)
    _seed_session("sescodex0001", anchor="slack_C777", workdir=workdir, backend="codex")

    provider = DurableSessionOwnershipProvider()
    reads = {"count": 0}

    class _AdmitsInsideTheLock:
        def snapshot(self):
            reads["count"] += 1
            if reads["count"] == 2:
                _seed_delivery("sescodex0001", "queued")
            return provider.snapshot()

    agent, calls = _codex_agent(workdir, ownership=_AdmitsInsideTheLock())

    assert _codex_sweep(agent, monkeypatch) == 0
    assert reads["count"] == 2, "the in-lock recheck must consult the union again"
    assert calls.stopped == []
    assert workdir in agent._transports


def test_a_spent_codex_pin_force_evicts_but_keeps_the_thread_mapping(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-145: past the cap the transport goes, the resume path stays.

    The Codex bound is ``max(idle_timeout * 3, 1800s)``, the same backstop that
    reaps a wedged app-server. Eviction stays loss-free because
    ``invalidate_thread`` drops only the in-memory binding: the persisted thread
    mapping survives, so the next dispatch resumes the same Codex conversation.
    """

    _state_db()
    workdir = str(tmp_path)
    _seed_session("sescodex0001", anchor="slack_C777", workdir=workdir, backend="codex")
    _seed_delivery("sescodex0001", "claimed")

    agent, calls = _codex_agent(workdir)
    agent._settle_stuck_active_request = _settle_recorder(calls)

    assert _codex_sweep(agent, monkeypatch, now=STUCK_THRESHOLD + 1.0) == 1
    assert calls.stopped == [workdir]
    assert calls.invalidated == ["codex-session-1"]
    agent.sessions.clear_agent_session_mapping.assert_not_called()
    assert workdir not in agent._transports


def _settle_recorder(calls):
    async def _settle(base_session_id: str) -> None:
        calls.cleared.append(f"settled:{base_session_id}")

    return _settle


def test_every_backend_eviction_consumer_is_inventoried() -> None:
    """HFR-146: a new eviction path may not ship without an ownership decision.

    The inventory is the contract, not a comment: idle eviction lives in exactly
    two places today, and both consult the interlock. OpenCode has no eviction
    consumer at all -- its runtime is not reaped on an idle clock -- so it is
    restart-safe by construction. If that changes, this test fails and the new
    path has to be classified here.
    """

    import inspect

    from modules.agents.opencode import agent as opencode_agent_module

    interlocked = {
        SessionHandler.evict_idle_sessions: session_handler_module,
        CodexAgent.evict_idle_transports: codex_agent_module,
    }
    for function in interlocked:
        source = inspect.getsource(function)
        assert "ownership" in source, f"{function.__qualname__} must consult the owner union"

    opencode_evictors = [
        name
        for name, _member in inspect.getmembers(opencode_agent_module.OpenCodeAgent, callable)
        if name.startswith("evict") or name.startswith("reap_idle")
    ]
    assert opencode_evictors == [], (
        "OpenCode grew an idle-eviction path: it must consume core/session_ownership.py "
        f"or prove restart-safety here -- found {opencode_evictors}"
    )


# ---------------------------------------------------------------------------
# HFR-148 … HFR-149: a pin must be scoped, and must not depend on an allowlist
# ---------------------------------------------------------------------------


def test_a_pin_does_not_cross_backends_at_a_shared_workdir(monkeypatch, tmp_path: Path) -> None:
    """HFR-148: durable work pins the runtime it will actually run in, only.

    Both keyspaces collapse across backends at the default working directory: a
    Codex transport is keyed by cwd alone, and a Claude runtime key is
    ``anchor:workdir``. So an unscoped pin lets a Claude-backed session hold a
    Codex app-server open for work that will never be dispatched into it, and
    vice versa -- an idle runtime kept alive forever by a neighbour's queue.
    Ownership is only evidence for the backend the durable work names.
    """

    _state_db()
    workdir = str(tmp_path)

    # A Claude-backed owner at this cwd is not evidence for the Codex transport.
    _seed_session("sesclaude001", anchor="slack_C888", workdir=workdir, backend="claude")
    _seed_delivery("sesclaude001", "queued")

    agent, calls = _codex_agent(workdir)
    assert _codex_sweep(agent, monkeypatch) == 1, "a foreign-backend owner must not pin Codex"
    assert calls.stopped == [workdir]

    # ... and the same owner on the matching backend still pins it.
    _state_db()
    _seed_session("sescodex0001", anchor="slack_C777", workdir=workdir, backend="codex")
    _seed_delivery("sescodex0001", "queued")

    agent, calls = _codex_agent(workdir)
    assert _codex_sweep(agent, monkeypatch) == 0
    assert calls.stopped == []


def test_a_codex_owner_does_not_pin_the_claude_runtime_at_the_same_key(
    monkeypatch, tmp_path: Path
) -> None:
    """HFR-148: the mirror direction, on the Claude consumer.

    A session that has switched backends keeps its anchor and its workdir, so its
    Claude runtime key is unchanged. Queued Codex work must not keep the stale
    SDK client from a previous backend alive.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path), backend="codex")
    _seed_delivery(BASE_SESSION_ID, "queued")

    assert _sweep(handler) == 1, "Codex-backed work must not pin the Claude runtime"
    assert composite_key not in controller.claude_sessions
    assert captured["disconnects"] == 1


def test_an_unrecognized_run_status_still_pins(monkeypatch, tmp_path: Path) -> None:
    """HFR-149: nonterminal is 'not terminal', never 'on the list we know about'.

    ``agent_runs.status`` carries no CHECK constraint and ``normalize_run_status``
    passes an unrecognized string through untouched, so a status introduced later
    -- or imported from another writer -- reaches this query. Selecting the known
    in-progress statuses would silently drop such a Run's interlock, which is the
    one failure mode this whole provider exists to prevent. It is queried as NOT
    IN the terminal set, exactly like Deliveries and Turns.
    """

    _state_db()
    handler, controller, composite_key, captured = _claude_handler(monkeypatch, tmp_path)
    _seed_session(BASE_SESSION_ID, anchor=ANCHOR, workdir=str(tmp_path))
    _seed_run(BASE_SESSION_ID, run_type="agent_run", status="retrying")

    assert _sweep(handler) == 0, "an unknown Run status must fail closed and pin"
    assert composite_key in controller.claude_sessions
    assert captured["disconnects"] == 0
