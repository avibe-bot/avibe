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
    from core.session_turns import _segment_dispatch_text

    payloads = store.list_queued(conn, "ses_fsm")
    queue = [store.get_delivery(conn, row["id"]) for row in payloads]
    turn_id = store.new_turn_id()
    store.claim_start_batch(
        conn, turn_id=turn_id, session_id="ses_fsm", backend="codex", deliveries=queue,
        dispatch_text=_segment_dispatch_text(payloads),
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
    hints = []
    original_run = manager._run

    async def capture(session_id, context, text, **kwargs):
        hints.append(list((context.platform_specific or {}).get("harness_hints") or []))
        await original_run(session_id, context, text, **kwargs)

    manager._run = capture

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
    # The reports' own text, once; the approved line comes first among the Harness lines.
    assert [text for _, text in starts] == ["long work", expected], (observed, queue)
    assert hints[-1][0] == STOP_LINE


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


def _complete(conn, turn_id):
    turn = store.get_turn(conn, turn_id)
    store.bind_native_start(conn, turn_id, expected_version=turn["version"], runtime_key=turn_id,
                            runtime_turn_id=turn_id, native_turn_id=turn_id)
    store.materialize_start_acceptance(conn, turn_id=turn_id, evidence={})
    store.terminalize_turn(conn, turn_id, outcome="completed", settled_by="terminal_result",
                           evidence_kind="terminal_result")


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
            select(messages.c.content_text, messages.c.metadata_json)
            .where(messages.c.id == store.get_turn(conn, successor)["initial_delivery_id"])
        ).one()
        sent = store.get_turn(conn, successor)["dispatch_text"]
    # The person sees the reports once, labelled as a carry in their language by the Web
    # (``turn_carry``); the Agent reads the approved line as a Harness line. Nothing
    # rewrites the inputs' text, and the stopped work is in neither.
    assert shown.content_text == "first report\nsecond report"
    assert json.loads(shown.metadata_json)["turn_carry"] == "stopped"
    assert sent == "first report\nsecond report"
    assert store.turn_carry_line([{"metadata": json.loads(shown.metadata_json)}]) == STOP_LINE


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "context_overflow", "invalid_request", None])
def test_a_failure_that_will_repeat_carries_the_reports_and_not_its_request(managers, kind):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="initial request"))
    _merge(manager, "collateral report")
    _merge(manager, "the person's own note", harness=False)
    with engine.begin() as conn:
        assert "automatic_retry_delivery_id" not in _end(conn, turn_id, kind=kind)
        assert _texts(conn) == ["collateral report"]
        assert store.turn_carry_line(store.list_queued(conn, "ses_fsm")) == (
            "The previous Turn failed before answering the user's last message and before handling "
            "the messages below."
        )
        successor = _start_next(conn)
        assert store.get_turn(conn, successor)["dispatch_text"] == "collateral report"
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
        # New work starts first and releases the parked report into the next Turn;
        # a parked report never wakes the Session itself.
        assert store.claimable_fifo_head(conn, "ses_fsm") is None
        store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text="new work")
        head = store.get_delivery(conn, store.claimable_fifo_head(conn, "ses_fsm")["id"])
        assert head["dispatch_text"] == "new work"
        assert not store.can_start_carried_batch(conn, "ses_fsm", [carried])
        fresh = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=fresh, session_id="ses_fsm", backend="codex",
                                deliveries=[head], dispatch_text="new work")
        _complete(conn, fresh)
        carried = store.get_delivery(conn, carried["id"])
        assert store.can_start_carried_batch(conn, "ses_fsm", [carried])
        nxt = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=nxt, session_id="ses_fsm", backend="codex",
                                deliveries=[carried], dispatch_text="report")
        turn = store.get_turn(conn, nxt)
        store.bind_native_start(conn, nxt, expected_version=turn["version"], runtime_key="renew",
                                runtime_turn_id="renew", native_turn_id="renew")
        store.materialize_start_acceptance(conn, turn_id=nxt, evidence={})
        _end(conn, nxt, kind=kind)
        # Parked again, waiting for the next new work: no loop on the old wake.
        assert store.claimable_fifo_head(conn, "ses_fsm") is None
        assert _run_delivery(conn, "run-report") == store.list_queued(conn, "ses_fsm")[0]["id"]


def test_new_work_sent_straight_into_an_idle_session_releases_parked_inputs(managers):
    """A message that starts a Turn without waiting in the queue (P1 into an idle
    Session, a replacement) releases parked inputs too (#4010 review)."""
    manager, _, engine, _, starts = managers
    turn_id, _ = asyncio.run(_activate(manager, text="human request"))
    _merge(manager, "report")
    with engine.begin() as conn:
        for attempt in range(4):
            _end(conn, turn_id, kind="network")
            if attempt < 3:
                turn_id = _start_next(conn)
        assert store.claimable_fifo_head(conn, "ses_fsm") is None
    asyncio.run(manager.deliver(DeliveryRequest(session_id="ses_fsm", priority="p1", content="fresh"), context=_context()))
    with engine.connect() as conn:
        parked = store.list_queued(conn, "ses_fsm")
        assert [row["dispatch_text"] for row in parked] == ["report"]
        assert store.can_start_carried_batch(conn, "ses_fsm", [store.get_delivery(conn, parked[0]["id"])])


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


def _fail_through_manager(manager, turn_id, kind):
    with manager._sqlite_engine().begin() as conn:
        _notice(conn, turn_id)
    return manager._terminalize_durable_turn(
        turn_id, "failed", settled_by="terminal_result", evidence_kind="terminal_result",
        evidence={"output_provenance": {"error_kind": kind, "turn_id": turn_id}, "settles_run": True},
        resume_successors=False,
    )


@pytest.mark.parametrize("sender", [
    {"callback_session_id": "ses_sender", "source_kind": "agent", "source_actor": "ses_sender"},
    # A report that arrived as another Session's callback names its sender in metadata only.
    {"source_kind": "callback", "source_actor": "parent", "metadata_json": '{"source_session_id": "ses_sender"}'},
])
def test_a_parked_input_tells_its_sender_once_not_to_resend(managers, sender):
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_sender")
    turn_id, _ = asyncio.run(_activate(manager, text="human request"))
    original = _merge(manager, "report")
    _run(engine, "run-report", original, **sender)
    notices = []
    for attempt in range(5):
        assert _fail_through_manager(manager, turn_id, "overloaded")["changed"]
        with engine.begin() as conn:
            notices.append(conn.execute(
                select(agent_runs.c.prompt, agent_runs.c.session_id).where(agent_runs.c.parent_run_id == "run-report")
            ).all())
            if attempt < 4:
                if attempt == 3:  # parked: new work starts a Turn and releases it
                    store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text="new work")
                    head = store.get_delivery(conn, store.claimable_fifo_head(conn, "ses_fsm")["id"])
                    assert head["dispatch_text"] == "new work"
                    fresh = store.new_turn_id()
                    store.claim_start_batch(conn, turn_id=fresh, session_id="ses_fsm", backend="codex",
                                            deliveries=[head], dispatch_text="new work")
                    _complete(conn, fresh)
                payloads = [p for p in store.list_queued(conn, "ses_fsm") if p["dispatch_text"] == "report"]
                assert store.claimable_fifo_head(conn, "ses_fsm")["id"] == payloads[0]["id"]
                turn_id = store.new_turn_id()
                store.claim_start_batch(conn, turn_id=turn_id, session_id="ses_fsm", backend="codex",
                                        deliveries=[store.get_delivery(conn, payloads[0]["id"])], dispatch_text="report")
                turn = store.get_turn(conn, turn_id)
                store.bind_native_start(conn, turn_id, expected_version=turn["version"], runtime_key="r",
                                        runtime_turn_id="r", native_turn_id="r")
                store.materialize_start_acceptance(conn, turn_id=turn_id, evidence={})
    # Nothing for the three successors, one notice at parking, none when it parks again.
    assert [len(rows) for rows in notices] == [0, 0, 0, 1, 1]
    # The approved sentence, then the sender's own input, by its original id.
    assert notices[-1] == [(f"{store.TURN_CARRY_PARKED_NOTICE}\n{original}", "ses_sender")]
    with engine.connect() as conn:
        assert conn.execute(select(agent_runs.c.status).where(agent_runs.c.id == "run-report")).scalar_one() == "running"


def _notices_to(engine, session_id):
    with engine.connect() as conn:
        return conn.execute(
            select(agent_runs.c.prompt, agent_runs.c.source_actor)
            .where(agent_runs.c.session_id == session_id, agent_runs.c.source_actor.like("%:turn-carry-%"))
        ).all()


def test_a_second_failure_tells_the_owner_and_the_sender_which_inputs(managers):
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_sender")
    turn_id, _ = asyncio.run(_activate(manager, text="human request"))
    report = _merge(manager, "report")
    _run(engine, "run-report", report, callback_session_id="ses_sender")
    _fail_through_manager(manager, turn_id, "auth")
    with engine.begin() as conn:
        successor = _start_next(conn)
    _fail_through_manager(manager, successor, "auth")
    with engine.connect() as conn:
        rows = conn.execute(select(messages.c.content_text, messages.c.metadata_json).where(messages.c.type == "notify")).all()
    owner = [row for row in rows if json.loads(row.metadata_json).get("event") == "turn_carry_exhausted"]
    assert len(owner) == 1 and report in owner[0].content_text
    assert json.loads(owner[0].metadata_json)["input_ids"] == [report]
    # The sender gets its own notice, once, naming its input; not a share of a coalesced callback.
    assert _notices_to(engine, "ses_sender") == [
        (store.TURN_CARRY_EXHAUSTED_NOTICE.format(inputs=report), "run-report:turn-carry-exhausted"),
    ]


def test_a_retry_press_is_not_blocked_or_made_stale_by_parked_inputs(managers):
    from core.backend_failure_retry import _validate_boundary, admission_denial
    from tests.backend_failure_retry_helpers import reserve_failure_retry

    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager, text="request"))
    _merge(manager, "report")
    with engine.begin() as conn:
        for _attempt in range(4):
            _end(conn, turn_id, kind="network")
            if _attempt < 3:
                turn_id = _start_next(conn)
        notice = conn.execute(select(messages).where(
            messages.c.type == "notify", messages.c.metadata_json.like(f"%{turn_id}%"),
        )).mappings().one()
        notice = messages_service.get_message(conn, notice["id"])
        assert _validate_boundary(conn, notice, exclude_ids=set())["id"] == turn_id
        retry = reserve_failure_retry(conn, notice)
        # Admission queues the press, as ``_admit_p3`` does.
        retry = store.cas_delivery(conn, retry["id"], expected_version=retry["version"],
                                   expected_states=("reserved",), values={"state": "queued"},
                                   history_event={"kind": "queue", "reason": "p3_admission"})
        assert admission_denial(conn, retry) is None
        # The press starts first and releases the parked report into the next Turn.
        assert store.claimable_fifo_head(conn, "ses_fsm")["id"] == retry["id"]
        pressed = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=pressed, session_id="ses_fsm", backend="codex",
                                deliveries=[store.get_delivery(conn, retry["id"])], dispatch_text="continue")
        carried = store.get_delivery(conn, store.claimable_fifo_head(conn, "ses_fsm")["id"])
        assert store.turn_carry(carried)
        _complete(conn, pressed)
        # The report runs next, in its own Turn; the press already counted.
        assert store.claimable_fifo_head(conn, "ses_fsm")["id"] == carried["id"]


def test_the_stopped_runner_does_not_cancel_the_runs_it_carried_on(managers):
    """The runner settles every Run its context names as ``canceled`` after a Stop
    (``_settle_turn_owned_agent_runs``); a carried report's Run is not its to end."""
    from storage.background import SQLiteBackgroundTaskStore
    from types import SimpleNamespace

    manager, _, engine, _, _ = managers
    turn_id, ctx = asyncio.run(_activate(manager, text="long work"))
    report = _merge(manager, "report")
    with engine.begin() as conn:
        initial = store.get_turn(conn, turn_id)["initial_delivery_id"]
    _run(engine, "run-work", initial)
    _run(engine, "run-report", report)
    background = SQLiteBackgroundTaskStore(Path(engine.url.database))
    settled = []

    def settle(run_ids, settled_by, **_options):
        settled.append(sorted(run_ids))
        return []

    manager.controller.scheduled_task_service = SimpleNamespace(settle_agent_runs_without_result=settle)
    try:
        with engine.begin() as conn:
            _end(conn, turn_id, stopped=True)
        ctx.platform_specific["turn_token"] = turn_id
        ctx.platform_specific["accepted_agent_run_ids"] = ["run-work", "run-report"]
        manager._settle_turn_owned_agent_runs(ctx, "stopped")
    finally:
        background.close()
    assert settled == [["run-work"]]


def test_a_carried_input_does_not_merge_with_a_fresh_one_from_the_same_sender():
    """The line says these arrived during the stopped Turn; a later report did not."""
    base = {"platform": "avibe", "author": "harness", "type": "harness", "source": "harness", "author_name": "agent_run"}
    carried = {**base, "metadata": {"turn_carry": "stopped"}}
    assert store.message_merge_identity(carried) != store.message_merge_identity({**base, "metadata": {}})
    assert store.message_merge_identity(carried) == store.message_merge_identity({**base, "metadata": {"turn_carry": "stopped"}})


# ----- #4010 review -----

def _record(engine, run_ids, turn_id, status, kind=None):
    from storage.background import SQLiteBackgroundTaskStore

    background = SQLiteBackgroundTaskStore(Path(engine.url.database))
    try:
        background.record_turn_terminal_outputs(
            run_ids, output_id=f"terminal:{turn_id}", text="", terminal_status=status,
            provenance={"turn_id": turn_id, **({"error_kind": kind} if kind else {})},
        )
    finally:
        background.close()
    with engine.connect() as conn:
        return dict(conn.execute(select(agent_runs.c.id, agent_runs.c.status).where(agent_runs.c.id.in_(run_ids))).all())


def test_a_late_stop_settlement_does_not_cancel_the_carried_run(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _run(engine, "run-report", _merge(manager, "report"))
    with engine.begin() as conn:
        _end(conn, turn_id, stopped=True)
    # The steer receipt path settles its pre-carry Run list from the stopped Turn.
    assert _record(engine, ["run-report"], turn_id, "canceled") == {"run-report": "running"}


def test_an_older_turn_output_cannot_settle_a_run_two_carries_on(managers):
    manager, _, engine, _, _ = managers
    t0, _ = asyncio.run(_activate(manager))
    _run(engine, "run-report", _merge(manager, "report"))
    with engine.begin() as conn:
        _end(conn, t0, kind="network")
        t1 = _start_next(conn)
        _end(conn, t1, kind="network")
    assert _record(engine, ["run-report"], t0, "failed", "network") == {"run-report": "running"}
    assert _record(engine, ["run-report"], t1, "failed", "network") == {"run-report": "running"}


@pytest.mark.parametrize("evidence_kind", ["restart_runtime_missing", "native_terminal"])
def test_failures_recorded_without_a_terminal_result_carry_too(managers, evidence_kind):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _merge(manager, "report")
    with engine.begin() as conn:
        store.terminalize_turn(conn, turn_id, outcome="failed", settled_by="restarted", evidence_kind=evidence_kind)
        assert _texts(conn) == ["report"]


def test_a_person_steering_into_a_carried_turn_makes_the_old_retry_stale(managers):
    from core.backend_failure_retry import RetryUnavailable, _validate_boundary

    manager, _, engine, _, _ = managers
    t0, _ = asyncio.run(_activate(manager, text="request"))
    _merge(manager, "report")
    with engine.begin() as conn:
        _end(conn, t0, kind="auth")
        notice_id = conn.execute(select(messages.c.id).where(messages.c.type == "notify")).scalar_one()
        notice = messages_service.get_message(conn, notice_id)
        t1 = _start_next(conn)
    manager._active_identity = lambda _b, _s, logical: (logical, f"native-{logical}")
    _merge(manager, "a new question", harness=False)
    with engine.begin() as conn:
        _complete_started(conn, t1)
        with pytest.raises(RetryUnavailable):
            _validate_boundary(conn, notice, exclude_ids=set())


def _complete_started(conn, turn_id):
    store.terminalize_turn(conn, turn_id, outcome="completed", settled_by="terminal_result",
                           evidence_kind="terminal_result")


def test_provider_count_starts_at_the_first_provider_failure(managers):
    """A report carried after a Stop gets its three provider successors in full."""
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _merge(manager, "report")
    with engine.begin() as conn:
        _end(conn, turn_id, stopped=True)
        for attempt in range(4):
            turn_id = _start_next(conn)
            _end(conn, turn_id, kind="overloaded")
            carried = store.get_delivery(conn, store.list_queued(conn, "ses_fsm")[0]["id"])
            assert store.waiting_for_external_input(carried) == (attempt == 3)


def test_one_sender_gets_one_notice_naming_all_its_inputs(managers):
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_sender")
    turn_id, _ = asyncio.run(_activate(manager))
    first, second = _merge(manager, "first report"), _merge(manager, "second report")
    _run(engine, "run-1", first, callback_session_id="ses_sender")
    _run(engine, "run-2", second, callback_session_id="ses_sender")
    _fail_through_manager(manager, turn_id, "auth")
    with engine.begin() as conn:
        successor = _start_next(conn)
    _fail_through_manager(manager, successor, "auth")
    [(prompt, _actor)] = _notices_to(engine, "ses_sender")
    assert prompt == store.TURN_CARRY_EXHAUSTED_NOTICE.format(inputs=f"{first}, {second}")


def test_the_carry_line_never_touches_the_input_text(managers):
    """``reviewer: …`` still routes, and ``12:30 UTC`` or a URL stays intact (#4010 review)."""
    from core.session_turns import _segment_dispatch_text

    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _merge(manager, "reviewer: inspect https://example.org/x at 12:30 UTC")
    with engine.begin() as conn:
        _end(conn, turn_id, kind="auth")
        queue = store.list_queued(conn, "ses_fsm")
    assert _segment_dispatch_text(queue) == "reviewer: inspect https://example.org/x at 12:30 UTC"
    assert store.turn_carry_line(queue) == store.TURN_CARRY_LINES["failed_user"]


def test_a_successful_output_from_an_older_turn_cannot_settle_a_carried_run(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _run(engine, "run-report", _merge(manager, "report"))
    with engine.begin() as conn:
        _end(conn, turn_id, stopped=True)
    assert _record(engine, ["run-report"], turn_id, "succeeded") == {"run-report": "running"}


def test_a_cancel_request_does_not_let_an_older_turn_settle_a_carried_run(managers):
    manager, _, engine, _, _ = managers
    t0, _ = asyncio.run(_activate(manager))
    _run(engine, "run-report", _merge(manager, "report"))
    with engine.begin() as conn:
        _end(conn, t0, kind="network")
        _start_next(conn)
        conn.execute(agent_runs.update().where(agent_runs.c.id == "run-report").values(cancel_requested=1))
    assert _record(engine, ["run-report"], t0, "failed", "network") == {"run-report": "running"}


def test_a_live_carried_turn_still_makes_the_old_retry_busy(managers):
    from core.backend_failure_retry import RetryUnavailable, _validate_boundary

    manager, _, engine, _, _ = managers
    t0, _ = asyncio.run(_activate(manager, text="request"))
    _merge(manager, "report")
    with engine.begin() as conn:
        _end(conn, t0, kind="auth")
        notice = messages_service.get_message(
            conn, conn.execute(select(messages.c.id).where(messages.c.type == "notify")).scalar_one()
        )
        t1 = _start_next(conn)
        with pytest.raises(RetryUnavailable):
            _validate_boundary(conn, notice, exclude_ids=set())
        _complete_started(conn, t1)
        assert _validate_boundary(conn, notice, exclude_ids=set())["id"] == t0


def test_notices_never_cause_notices_or_wake_parked_inputs(managers):
    """Two Sessions whose models both fail must not keep each other busy (#4010 review)."""
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_sender")
    turn_id, _ = asyncio.run(_activate(manager))
    _merge(manager, "report")
    with engine.begin() as conn:
        for attempt in range(4):
            _end(conn, turn_id, kind="network")
            if attempt < 3:
                turn_id = _start_next(conn)
        parked = store.list_queued(conn, "ses_fsm")[0]["id"]
    # A notice from another Session arrives; it is carried like any report, but it
    # neither releases the parked report nor produces a notice of its own.
    notice = _merge_into_idle(manager, engine, "Not handled yet: ...", "ses_sender:turn-carry-parked")
    with engine.connect() as conn:
        assert store.waiting_for_external_input(store.get_delivery(conn, parked))
        assert not store._is_new_work(conn, store.get_delivery(conn, notice))


def _merge_into_idle(manager, engine, text, actor):
    with engine.begin() as conn:
        row = store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text=text)
    _run(engine, f"run-{actor}", row["id"], source_kind="callback", source_actor=actor)
    with engine.begin() as conn:
        store.claim_start_batch(conn, turn_id=store.new_turn_id(), session_id="ses_fsm", backend="codex",
                                deliveries=[store.get_delivery(conn, row["id"])], dispatch_text=text)
    return row["id"]


def test_a_newer_turn_whose_inputs_left_it_still_makes_the_retry_stale(managers):
    """A Turn stopped before its write retires its input; it was still newer work."""
    from core.backend_failure_retry import RetryUnavailable, _validate_boundary

    manager, _, engine, _, _ = managers
    t0, _ = asyncio.run(_activate(manager, text="request"))
    with engine.begin() as conn:
        _end(conn, t0, kind="auth")
        notice = messages_service.get_message(
            conn, conn.execute(select(messages.c.id).where(messages.c.type == "notify")).scalar_one()
        )
        row = store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text="newer")
        newer = store.new_turn_id()
        store.claim_start_batch(conn, turn_id=newer, session_id="ses_fsm", backend="codex",
                                deliveries=[store.get_delivery(conn, row["id"])], dispatch_text="newer")
        store.terminalize_turn(conn, newer, outcome="not_written", settled_by="stopped", evidence_kind="prewrite")
        conn.execute(store.message_deliveries.update().where(store.message_deliveries.c.id == row["id"]).values(
            state="retired", turn_id=None, turn_role=None, turn_position=None, retired_at=store.utc_now_iso(),
        ))
        with pytest.raises(RetryUnavailable):
            _validate_boundary(conn, notice, exclude_ids=set())


def test_a_retry_that_reuses_a_carried_input_releases_the_parked_ones(managers):
    manager, _, engine, _, _ = managers
    turn_id, _ = asyncio.run(_activate(manager))
    _merge(manager, "report")
    with engine.begin() as conn:
        for attempt in range(4):
            _end(conn, turn_id, kind="network")
            if attempt < 3:
                turn_id = _start_next(conn)
        parked = store.get_delivery(conn, store.list_queued(conn, "ses_fsm")[0]["id"])
        other = store.enqueue_queued(conn, scope_id=None, session_id="ses_fsm", text="other carried")
        other = store.cas_delivery(conn, other["id"], expected_version=other["version"], expected_states=("queued",),
                                   values={}, history_event={"kind": store.TURN_CARRY_HISTORY_KIND, "source_turn_id": "x"})
        other = store.cas_delivery(conn, other["id"], expected_version=other["version"], expected_states=("queued",),
                                   values={}, history_event={"kind": store.FAILURE_RETRY_HISTORY_KIND, "notice_id": "n",
                                                             "source_turn_id": "x", "delivery_ids": [other["id"]]})
        store.claim_start_batch(conn, turn_id=store.new_turn_id(), session_id="ses_fsm", backend="codex",
                                deliveries=[other], dispatch_text="other carried")
        assert not store.waiting_for_external_input(store.get_delivery(conn, parked["id"]))



def test_notices_split_by_authority_and_skip_internal_notices(managers):
    """Two reports from one Session with different authority get separate notices,
    each with its own Run's authority; an internal notice is named to nobody."""
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_sender")
    turn_id, _ = asyncio.run(_activate(manager))
    local, remote = _merge(manager, "local report"), _merge(manager, "remote report")
    internal = _merge(manager, "Not handled yet: ...")
    _run(engine, "run-local", local, callback_session_id="ses_sender")
    _run(engine, "run-remote", remote, callback_session_id="ses_sender",
         metadata_json=json.dumps({"resource_user_context": {"user_key": "remote:u1"}}))
    _run(engine, "run-internal", internal, source_kind="callback", source_actor="x:turn-carry-parked",
         metadata_json=json.dumps({"source_session_id": "ses_sender"}))
    _fail_through_manager(manager, turn_id, "auth")
    with engine.begin() as conn:
        successor = _start_next(conn)
    _fail_through_manager(manager, successor, "auth")
    with engine.connect() as conn:
        notices = conn.execute(
            select(agent_runs.c.prompt, agent_runs.c.metadata_json)
            .where(agent_runs.c.session_id == "ses_sender", agent_runs.c.source_actor.like("%:turn-carry-%"))
        ).all()
        owner = conn.execute(select(messages.c.metadata_json).where(messages.c.type == "notify")).scalars().all()
    by_input = {
        prompt.rsplit(": ", 1)[-1]: json.loads(metadata).get("resource_user_context") for prompt, metadata in notices
    }
    assert by_input == {local: None, remote: {"user_key": "remote:u1"}}
    exhausted = [json.loads(m) for m in owner if json.loads(m).get("event") == "turn_carry_exhausted"]
    assert exhausted and internal not in exhausted[0]["input_ids"]


def test_a_task_from_another_session_tells_its_creator(managers):
    from tests.test_session_delivery_fsm import _seed_session

    manager, _, engine, _, _ = managers
    _seed_session(engine, "ses_creator")
    turn_id, _ = asyncio.run(_activate(manager))
    # As production enqueues a Task: the Run drops the definition's authority, the
    # admitted input carries it (``drop_definition_resource_context``).
    manager._steer = AsyncMock(return_value=steer_result(SteerOutcome.ACCEPTED))
    report = str(asyncio.run(manager.deliver(DeliveryRequest(
        session_id="ses_fsm", priority="p1", content="task output", source="harness", author="harness",
        message_type="harness", metadata={"resource_user_context": {"user_key": "remote:creator"}},
    ), context=_context())).delivery_id)
    _run(engine, "run-task", report, run_type="scheduled", source_kind="scheduler", source_actor="task_1",
         metadata_json=json.dumps({"created_by": {"kind": "caller_context", "caller": {"session_id": "ses_creator"}}}))
    _fail_through_manager(manager, turn_id, "auth")
    with engine.begin() as conn:
        successor = _start_next(conn)
    _fail_through_manager(manager, successor, "auth")
    assert _notices_to(engine, "ses_creator") == [
        (store.TURN_CARRY_EXHAUSTED_NOTICE.format(inputs=report), "run-task:turn-carry-exhausted"),
    ]
    with engine.connect() as conn:
        metadata = conn.execute(
            select(agent_runs.c.metadata_json).where(agent_runs.c.source_actor == "run-task:turn-carry-exhausted")
        ).scalar_one()
    assert json.loads(metadata)["resource_user_context"] == {"user_key": "remote:creator"}
