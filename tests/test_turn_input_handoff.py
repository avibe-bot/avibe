"""What a failed or stopped Turn accepted is handled by a Turn, once.

A message another Session, a Task or a Watch sends joins the running Turn. When that
Turn dies on a model error or is stopped, the message used to end with it: its
sender waited, or resent it by hand.
"""
import asyncio
import json
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
        assert shown == f"{FAILED_LINE}\n\ncollateral report"
        # The carried input had its one replay: a second failure sends nothing again.
        _end(conn, successor, kind=kind)
        assert _texts(conn) == []


@pytest.mark.parametrize("kind", ["server", "network", "overloaded", "stalled"])
def test_a_failure_that_can_pass_presses_its_own_retry_once_and_sends_no_text_again(managers, kind):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    report = _merge(manager, "collateral report")
    with engine.begin() as conn:
        initial = store.get_turn(conn, turn_id)["initial_delivery_id"]
    _run(engine, "run-initial", initial)
    _run(engine, "run-report", report)
    _run(engine, "run-called-off", report, cancel_requested=1)
    with engine.begin() as conn:
        before = conn.execute(select(messages.c.id)).scalars().all()
        retry_id = _end(conn, turn_id, kind=kind)["automatic_retry_delivery_id"]
        retry = store.get_delivery(conn, retry_id)
        # The Turn's own Retry, as a person's press reserves it: the notice names it.
        notice = conn.execute(select(messages.c.content_json).where(messages.c.type == "notify")).scalar_one()
        assert json.loads(notice)["failure_retry"] == {"delivery_id": retry_id}
        assert store.failure_retry_binding(retry)["source_turn_id"] == turn_id
        assert retry["dispatch_text"] == "continue"
        assert _texts(conn) == []  # nothing waits as text; the continuation is still a reservation
        assert _run_delivery(conn, "run-initial") == _run_delivery(conn, "run-report") == retry_id
        assert _run_delivery(conn, "run-called-off") == report
        assert sorted(conn.execute(select(messages.c.id)).scalars().all()) == sorted(before + [])
    asyncio.run(manager.deliver(
        DeliveryRequest(session_id="ses_fsm", priority="p3", content="continue", delivery_id=retry_id),
        context=_context(),
    ))
    with engine.begin() as conn:
        continued = store.get_delivery(conn, retry_id)["turn_id"]
        assert continued and continued != turn_id
        # The continued Turn fails too: no second Retry, and no text either.
        assert "automatic_retry_delivery_id" not in _end(conn, continued, kind=kind)
        assert _texts(conn) == []
        assert _run_delivery(conn, "run-report") == retry_id


def test_a_retry_the_notice_cannot_offer_still_carries_the_reports(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    report = _merge(manager, "collateral report")
    with engine.begin() as conn:
        initial = store.get_turn(conn, turn_id)["initial_delivery_id"]
    _run(engine, "run-initial", initial)
    _run(engine, "run-report", report)
    with engine.begin() as conn:
        assert "automatic_retry_delivery_id" not in _end(conn, turn_id, kind="overloaded", notice=False)
        assert _texts(conn) == ["collateral report"]
        assert _run_delivery(conn, "run-report") == store.list_queued(conn, "ses_fsm")[0]["id"]
        # The request's own Run stays with the ended Turn, which settles it.
        assert _run_delivery(conn, "run-initial") == initial


@pytest.mark.parametrize("kind,carried", [("overloaded", {"run-initial", "run-report"}), ("auth", {"run-report"})])
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
