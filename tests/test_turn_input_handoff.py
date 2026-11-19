"""What a failed or stopped Turn accepted is handled by a Turn, once.

A message another Session, a Task or a Watch sends joins the running Turn. When that
Turn dies on a model error or is stopped, the message used to end with it: its
sender waited, or resent it by hand.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from core.services.agent_steering import SteerOutcome, result as steer_result
from core.session_turns import DeliveryRequest
from storage import message_deliveries as store, messages_service
from storage.models import agent_runs, messages
from tests.test_session_delivery_fsm import _activate, _context, _fsm_schema_template, managers  # noqa: F401

STOP_LINE = (
    "The user stopped the previous Turn. The messages below arrived during it. "
    "Do not resume the stopped work unless one of them asks for it."
)
FAILED_LINE = "The previous Turn failed before handling the messages below."


def _merge(manager, text, *, harness=True):
    """One input accepted into the running Turn: a report from elsewhere, or the person's own."""
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    source = {"source": "harness", "author": "harness", "message_type": "harness"} if harness else {}
    accepted = asyncio.run(manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p1", content=text, **source), context=_context(),
    ))
    assert accepted.state == "accepted"
    return str(accepted.delivery_id)


def _run(engine, run_id, delivery_id, **values):
    now = store.utc_now_iso()
    with engine.begin() as conn:
        conn.execute(agent_runs.insert().values(**{
            "id": run_id, "run_type": "agent_run", "session_id": "ses_fsm", "status": "running",
            "created_at": now, "updated_at": now, "delivery_id": delivery_id, "cancel_requested": 0,
            "metadata_json": "{}", **values,
        }))


def _notice(conn, turn_id):
    return messages_service.append(
        conn, session_id="ses_fsm", scope_id=None, platform="avibe", author="agent", source="agent",
        message_type="notify", text="Backend interrupted",
        metadata={"event": "backend_failure", "backend": "codex", "turn_id": turn_id, "failure_id": f"turn:{turn_id}"},
    )


def _end(conn, turn_id, *, kind=None, stopped=False, notice=True):
    if notice and not stopped:
        _notice(conn, turn_id)
    return store.terminalize_turn(
        conn, turn_id, outcome="canceled" if stopped else "failed",
        settled_by="stopped" if stopped else "terminal_result", evidence_kind="terminal_result",
        evidence={"output_provenance": {"error_kind": kind, "turn_id": turn_id}, "settles_run": True},
    )


def _texts(conn):
    return [row["dispatch_text"] for row in store.list_queued(conn, "ses_fsm")]


def _run_delivery(conn, run_id):
    return conn.execute(select(agent_runs.c.delivery_id).where(agent_runs.c.id == run_id)).scalar_one()


def _start_next(conn):
    """The queue's next Turn takes everything waiting, as a drain would."""
    queue = [store.get_delivery(conn, row["id"]) for row in store.list_queued(conn, "ses_fsm")]
    turn_id = store.new_turn_id()
    store.claim_start_batch(
        conn, turn_id=turn_id, session_id="ses_fsm", backend="codex", deliveries=queue,
        dispatch_text="\n".join(row["dispatch_text"] for row in queue),
    )
    turn = store.get_turn(conn, turn_id)
    store.bind_native_start(
        conn, turn_id, expected_version=turn["version"], runtime_key="next", runtime_turn_id="next",
        native_turn_id="next",
    )
    store.materialize_start_acceptance(conn, turn_id=turn_id, evidence={})
    return turn_id


@pytest.mark.parametrize("cancel_one", [False, True])
def test_manager_stop_dispatches_surviving_reports_once(managers, cancel_one):
    from core.session_turns import Turn

    manager, _, engine, _, starts = managers

    async def exercise():
        turn_id, ctx = await _activate(manager, text="long work")
        manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
        for name in ("first", "second"):
            accepted = await manager.deliver(DeliveryRequest(
                session_id="ses_fsm", priority="p1", content=name, source="harness", author="harness", message_type="harness",
            ), context=_context())
            _run(engine, name, accepted.delivery_id)
        holder = asyncio.create_task(asyncio.Event().wait())
        manager.in_flight["ses_fsm"] = Turn(task=holder, context=ctx, logical_turn_id=turn_id)
        try:
            stopped = await manager.cancel("ses_fsm", **({"agent_run_id": "first"} if cancel_one else {}))
            assert stopped["ok"], stopped
            terminal = manager._terminalize_durable_turn(turn_id, "canceled", settled_by="stopped", evidence_kind="terminal_result")
            with engine.connect() as conn:
                ended = store.get_turn(conn, turn_id)
                assert ended["settled_by"] == "stopped", (stopped, terminal, ended["settled_by"])
            assert not manager._terminalize_durable_turn(turn_id, "completed", settled_by="terminal_result", evidence_kind="terminal_result")["changed"]
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)
            manager.in_flight.pop("ses_fsm", None)
        await manager._resume_post_terminal("ses_fsm")
        # A Run cancellation can already have scheduled the owning runner.
        # Join its dispatch rather than closing the test loop underneath it.
        for _ in range(30):
            if len(starts) > 1:
                break
            await asyncio.sleep(0.01)

    asyncio.run(exercise())
    expected = "second" if cancel_one else "first\nsecond"
    with engine.connect() as conn:
        observed = [(r.id, r.status, r.cancel_requested, r.delivery_id) for r in conn.execute(select(agent_runs))]
        queue = store.list_queued(conn, "ses_fsm")
    assert [text for _, text in starts] == ["long work", STOP_LINE + "\n\n" + expected], (observed, queue)


def test_late_acceptance_of_report_after_stop_queues_one_copy(managers):
    manager, other, engine, _, _ = managers

    async def exercise():
        turn_id, _ = await _activate(manager)
        entered, release = asyncio.Event(), asyncio.Event()

        async def accept(*_):
            entered.set()
            await release.wait()
            return steer_result(SteerOutcome.ACCEPTED)

        manager._steer = accept
        receipt = asyncio.create_task(manager.deliver(DeliveryRequest(
            session_id="ses_fsm", priority="p1", content="late report", source="harness", author="harness", message_type="harness",
        ), context=_context()))
        await entered.wait()
        other._terminalize_durable_turn(turn_id, "canceled", settled_by="stopped", evidence_kind="terminal_result", resume_successors=False)
        release.set()
        assert (await receipt).state == "accepted"

    asyncio.run(exercise())
    with engine.connect() as conn:
        assert _texts(conn) == ["late report"]


def test_a_stopped_turn_carries_what_other_sessions_sent_and_nothing_else(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="selected work"))
    first = _merge(manager, "first report")
    _merge(manager, "the person's own note", harness=False)
    _merge(manager, "second report")
    _run(engine, "run-first", first)
    for _ in range(2):  # a repeated terminal write carries nothing twice
        with engine.begin() as conn:
            _end(conn, turn_id, stopped=True)
    with engine.begin() as conn:
        assert _texts(conn) == ["first report", "second report"]
        carried = store.list_queued(conn, "ses_fsm")
        # The sender's Run goes on with the carried input, and the original keeps its Turn.
        assert _run_delivery(conn, "run-first") == carried[0]["id"]
        assert all(row["state"] == "accepted" for row in store.deliveries_for_turn(conn, turn_id))
        successor = _start_next(conn)
        shown = conn.execute(
            select(messages.c.content_text).where(messages.c.id == store.get_turn(conn, successor)["initial_delivery_id"])
        ).scalar_one()
    # Visibly a carry, once, and the stopped work is not in it.
    assert shown == f"{STOP_LINE}\n\nfirst report\nsecond report"


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "context_overflow", "invalid_request", None])
def test_a_failure_that_will_repeat_carries_the_reports_and_not_its_request(managers, kind):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    _merge(manager, "collateral report")
    _merge(manager, "the person's own note", harness=False)
    with engine.begin() as conn:
        assert "automatic_retry_delivery_id" not in _end(conn, turn_id, kind=kind)
        assert _texts(conn) == ["collateral report"]
        successor = _start_next(conn)
        shown = conn.execute(
            select(messages.c.content_text).where(messages.c.id == store.get_turn(conn, successor)["initial_delivery_id"])
        ).scalar_one()
        assert shown == "The previous Turn failed before answering the user's last message and before handling the messages below.\n\ncollateral report"
        # The carried input had its one replay: a second failure sends nothing again.
        _end(conn, successor, kind=kind)
        assert _texts(conn) == []


@pytest.mark.parametrize("kind", ["server", "network", "overloaded", "stalled"])
def test_provider_failure_parks_after_three_successors_without_losing_sender_run(managers, kind):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="human request"))
    original = _merge(manager, "report")
    _run(engine, "run-report", original)
    with engine.begin() as conn:
        for attempt in range(4):
            _end(conn, turn_id, kind=kind)
            queue = store.list_queued(conn, "ses_fsm")
            assert len(queue) == 1
            carried = store.get_delivery(conn, queue[0]["id"])
            assert _run_delivery(conn, "run-report") == carried["id"]
            assert store.turn_carry(carried)["original_id"] == original
            assert store.can_start_carried_batch(conn, "ses_fsm", [carried]) == (attempt < 3)
            if attempt < 3:
                turn_id = _start_next(conn)
        # Put one waiting request on the queue before the final failure. It must
        # still be able to start, but cannot pay for more retries of the report.
        store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text="new work")
        assert store.can_start_carried_batch(conn, "ses_fsm", [carried])
        # A normal queue would split these incompatible senders. Start the
        # report's segment, leaving the external wake queued as in production.
        nxt = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=nxt, session_id="ses_fsm", backend="codex",
                                deliveries=[carried], dispatch_text="report")
        turn = store.get_turn(conn, nxt)
        store.bind_native_start(conn, nxt, expected_version=turn["version"], runtime_key="renew",
                                runtime_turn_id="renew", native_turn_id="renew")
        store.materialize_start_acceptance(conn, turn_id=nxt, evidence={})
        _end(conn, nxt, kind=kind)
        head = store.claimable_fifo_head(conn, "ses_fsm")
        assert head and head["dispatch_text"] == "new work"
        assert [r["dispatch_text"] for r in store.claimable_fifo_prefix(conn, "ses_fsm")] == ["new work"]


@pytest.mark.parametrize("kind,carried", [("overloaded", {"run-report"}), ("auth", {"run-report"})])
def test_the_failing_turn_does_not_settle_a_run_that_goes_on(managers, kind, carried):
    from storage.background import SQLiteBackgroundTaskStore

    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    report = _merge(manager, "collateral report")
    with engine.begin() as conn:
        initial = store.get_turn(conn, turn_id)["initial_delivery_id"]
    _run(engine, "run-initial", initial)
    _run(engine, "run-report", report)
    background = SQLiteBackgroundTaskStore(Path(engine.url.database))
    provenance = {"turn_id": turn_id, "error_kind": kind}

    def record():
        background.record_turn_terminal_outputs(
            ["run-initial", "run-report"], output_id="terminal", text="failed", terminal_status="failed",
            provenance=provenance,
        )
        with engine.connect() as conn:
            return {
                row.id for row in conn.execute(select(agent_runs.c.id, agent_runs.c.status)) if row.status == "running"
            }

    try:
        # The failure is recorded before the Turn ends, and may be recorded again after.
        assert record() == carried
        with engine.begin() as conn:
            _end(conn, turn_id, kind=kind)
        assert record() == carried
    finally:
        background.close()
