"""Unit tests for the controller-side ``InboxEventBus`` fan-out.

The bus is the Controller-process half of the realtime inbox bridge:
``core.message_mirror`` publishes ``inbox.session.updated`` here, and
``core.internal_server``'s ``GET /internal/events`` subscribes and streams the
events over the dispatch socket to the UI server. These tests pin the contract
the bridge relies on: subscribers receive published events, unsubscribe stops
delivery, and a publish with no subscribers is a harmless no-op.

The repo has no ``pytest-asyncio``; following the existing convention
(``tests/test_dispatcher_stream_chunk.py``) each async scenario runs inside
``asyncio.run`` so the loop captured at ``subscribe`` time is the one driving
``publish``'s ``call_soon_threadsafe``.

The second half of the file (HFR-115 … HFR-119, PR2) is the row-level contract of
the session-teardown reconciler — the writer whose every settlement publishes on
the bus above. It lives here rather than beside the reconciler's selection tests
(``tests/test_scheduled_tasks.py``) because the subject is different: those pin
WHICH rows a teardown may settle, these pin what a settled row and its callback
look like afterwards.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy import event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.inbox_events import InboxEventBus


def test_publish_delivers_to_subscriber():
    async def scenario():
        bus = InboxEventBus()
        sub_id, queue = bus.subscribe()
        bus.publish("inbox.session.updated", {"session_id": "s1"})
        event_type, data = await asyncio.wait_for(queue.get(), timeout=1.0)
        bus.unsubscribe(sub_id)
        return event_type, data

    event_type, data = asyncio.run(scenario())
    assert event_type == "inbox.session.updated"
    assert data == {"session_id": "s1"}


def test_fanout_to_every_subscriber():
    async def scenario():
        bus = InboxEventBus()
        _, q1 = bus.subscribe()
        _, q2 = bus.subscribe()
        bus.publish("e", {"n": 1})
        return (
            await asyncio.wait_for(q1.get(), timeout=1.0),
            await asyncio.wait_for(q2.get(), timeout=1.0),
        )

    a, b = asyncio.run(scenario())
    assert a == ("e", {"n": 1})
    assert b == ("e", {"n": 1})


def test_unsubscribe_stops_delivery():
    async def scenario():
        bus = InboxEventBus()
        sub_id, queue = bus.subscribe()
        bus.unsubscribe(sub_id)
        bus.publish("inbox.session.updated", {"session_id": "s1"})
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

    asyncio.run(scenario())


def test_publish_without_subscribers_is_noop():
    # No loop captured, no subscribers — must not raise (boot / headless path).
    InboxEventBus().publish("inbox.session.updated", {"x": 1})


def test_synchronous_callback_runs_before_queue_fanout_and_is_unsubscribed():
    async def scenario():
        bus = InboxEventBus()
        order = []
        callback_id = bus.subscribe_callback(lambda event_type, data: order.append((event_type, data)))
        _, queue = bus.subscribe()
        bus.publish("turn.start", {"session_id": "s1"})
        assert order == [("turn.start", {"session_id": "s1"})]
        queued = await asyncio.wait_for(queue.get(), timeout=1.0)
        bus.unsubscribe(callback_id)
        bus.publish("turn.end", {"session_id": "s1"})
        return order, queued

    order, queued = asyncio.run(scenario())
    assert order == [("turn.start", {"session_id": "s1"})]
    assert queued == ("turn.start", {"session_id": "s1"})


def test_synchronous_callback_failure_does_not_break_queue_delivery():
    async def scenario():
        bus = InboxEventBus()

        def fail(_event_type, _data):
            raise RuntimeError("checkpoint failed")

        bus.subscribe_callback(fail)
        _, queue = bus.subscribe()
        bus.publish("turn.end", {"session_id": "s1"})
        return await asyncio.wait_for(queue.get(), timeout=1.0)

    assert asyncio.run(scenario()) == ("turn.end", {"session_id": "s1"})


def test_sqlite_background_store_publishes_run_updates(tmp_path):
    async def scenario():
        from core import inbox_events
        from storage.background import SQLiteBackgroundTaskStore

        sub_id, queue = inbox_events.bus.subscribe()
        store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
        try:
            store.enqueue_run(
                {
                    "id": "run_evt_1",
                    "request_type": "agent_run",
                    "status": "queued",
                    "message": "hello",
                    "created_at": "2026-07-04T00:00:00+00:00",
                    "updated_at": "2026-07-04T00:00:00+00:00",
                    "session_id": "ses_evt",
                }
            )
            queued = await asyncio.wait_for(queue.get(), timeout=1.0)

            claimed = store.claim_pending_run("run_evt_1", started_at="2026-07-04T00:00:01+00:00")
            assert claimed is not None
            running = await asyncio.wait_for(queue.get(), timeout=1.0)

            store.update_run_status(
                "run_evt_1",
                status="failed",
                updated_at="2026-07-04T00:00:02+00:00",
                completed_at="2026-07-04T00:00:02+00:00",
                error="boom",
            )
            failed = await asyncio.wait_for(queue.get(), timeout=1.0)
            return queued, running, failed
        finally:
            store.close()
            inbox_events.bus.unsubscribe(sub_id)

    queued, running, failed = asyncio.run(scenario())
    assert queued == (
        "runs.updated",
        {
            "run_id": "run_evt_1",
            "status": "queued",
            "run_type": "agent_run",
            "session_id": "ses_evt",
            "updated_at": "2026-07-04T00:00:00+00:00",
            "cancel_requested": False,
        },
    )
    assert running[0] == "runs.updated"
    assert running[1]["run_id"] == "run_evt_1"
    assert running[1]["status"] == "running"
    assert failed[0] == "runs.updated"
    assert failed[1]["run_id"] == "run_evt_1"
    assert failed[1]["status"] == "failed"


def test_sqlite_background_store_bridges_run_updates_without_local_subscribers(tmp_path, monkeypatch):
    from core import inbox_events
    from storage.background import SQLiteBackgroundTaskStore

    bridged = []
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", False)
    monkeypatch.setattr(
        "vibe.internal_client.publish_event_sync",
        lambda event_type, data, **kwargs: bridged.append((event_type, data, kwargs)),
    )
    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        store.enqueue_run(
            {
                "id": "run_evt_bridge",
                "request_type": "agent_run",
                "status": "queued",
                "message": "hello",
                "created_at": "2026-07-04T00:00:00+00:00",
                "updated_at": "2026-07-04T00:00:00+00:00",
            }
        )
    finally:
        store.close()

    assert bridged == [
        (
            "runs.updated",
            {
                "run_id": "run_evt_bridge",
                "status": "queued",
                "run_type": "agent_run",
                "updated_at": "2026-07-04T00:00:00+00:00",
                "cancel_requested": False,
            },
            {"timeout": 1.5},
        )
    ]


def test_sqlite_background_store_does_not_bridge_controller_self_updates(tmp_path, monkeypatch):
    from core import inbox_events
    from storage.background import SQLiteBackgroundTaskStore

    bridged = []
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", True)
    monkeypatch.setattr(
        "vibe.internal_client.publish_event_sync",
        lambda event_type, data, **kwargs: bridged.append((event_type, data, kwargs)),
    )
    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        store.enqueue_run(
            {
                "id": "run_evt_controller",
                "request_type": "agent_run",
                "status": "queued",
                "message": "hello",
                "created_at": "2026-07-04T00:00:00+00:00",
                "updated_at": "2026-07-04T00:00:00+00:00",
            }
        )
    finally:
        store.close()

    assert bridged == []


# --- teardown reconcile: the row and its callback (HFR-115 … HFR-119) -------

#: The session being torn down in every scenario below.
_TEARDOWN_SESSION = "ses-teardown-reconcile"


def _reconcile_fixture(tmp_path):
    """A real SQLite store plus the minimum service the reconciler actually uses.

    ``ScheduledTaskService.__new__`` rather than the constructor, following
    ``tests/test_harness_failure_visibility.py``'s ``_drain_service``: the
    reconciler touches exactly three attributes, and building the whole service
    would drag a controller, a scheduler and a definition store into a test about
    one guarded write.
    """

    from core.scheduled_tasks import ScheduledTaskService, TaskExecutionStore
    from storage.background import SQLiteBackgroundTaskStore

    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    requests = TaskExecutionStore(tmp_path / "task_requests")
    requests._sqlite = sqlite

    service = ScheduledTaskService.__new__(ScheduledTaskService)
    service.request_store = requests
    service._drain_dirty = False
    service._t = lambda key, **kwargs: key
    return sqlite, requests, service


def _running_run_on_the_torn_down_session(requests, sqlite, *, callback_session_id):
    """A claimed, ``running`` run associated with the session about to be torn down.

    Through the real enqueue/claim pair, not a hand-written row: the reconciler's
    predicate reads ``status`` and ``session_id`` and its writer re-reads the same
    row, so a staged row that skipped the claim would prove the query and not the
    transition.
    """

    run = requests.enqueue_agent_run(
        session_key="slack::channel::C1",
        session_id=_TEARDOWN_SESSION,
        message="a turn the session teardown interrupts",
        agent_name=None,
        callback_session_id=callback_session_id,
    )
    assert requests.claim(run.id) is not None
    staged = sqlite.get_run(run.id)
    assert staged["status"] == "running"
    assert staged["session_id"] == _TEARDOWN_SESSION
    return run.id


def _reconcile(service, run_id):
    from core.run_settlement import SETTLED_BY_EVICTED

    return service.reconcile_session_teardown(
        _TEARDOWN_SESSION,
        settled_by=SETTLED_BY_EVICTED,
        claimed_run_ids=frozenset({run_id}),
    )


def test_teardown_reconcile_terminalizes_a_running_row(tmp_path):
    """HFR-115: the interrupted row must end ``failed``, and it must end.

    ``failed``, not ``canceled`` — the §3.3 correction and the HFR-012/HFR-037
    guardrails. An eviction is an infrastructure fault; recording it as a
    cancellation claims the user asked for it, suppresses the interruption notice
    they are owed, and makes the failure accounting read as deliberate intent.

    ``completed_at`` is asserted separately from ``status`` because it is not
    decoration: every downstream reader that decides whether a run is finished —
    the callback drain's eligibility query included — filters on it, so a row
    marked terminal without one is still an open run everywhere it matters.
    """

    sqlite, requests, service = _reconcile_fixture(tmp_path)
    run_id = _running_run_on_the_torn_down_session(
        requests, sqlite, callback_session_id="ses-callback"
    )

    assert _reconcile(service, run_id) == 1

    settled = sqlite.get_run(run_id)
    assert settled["status"] == "failed"
    assert settled["completed_at"] is not None
    assert settled["error"]
    assert settled["metadata"]["interrupt_reason"] == "evicted"


def test_teardown_reconcile_leaves_the_callback_pending_for_the_drain(tmp_path):
    """HFR-116: settling the run is half the job; somebody asked to be told.

    The reconcile writes a terminal row from OUTSIDE the run's own execution, so
    the follow-through that normally rides on ``complete()`` does not happen here.
    That makes ``callback_status`` easy to get wrong in both directions: delivering
    inline from the settlement would put a network call inside a teardown, and
    clearing the field would silently retire the request. Neither is right — the
    row is handed to the callback drain exactly as a normal completion would be.

    Asserted through ``list_pending_callbacks`` rather than on the column, because
    the column being ``pending`` proves nothing if the row fails the drain's other
    predicates (terminal status AND ``completed_at``); discoverability is the
    property the user's notification actually depends on.
    """

    sqlite, requests, service = _reconcile_fixture(tmp_path)
    run_id = _running_run_on_the_torn_down_session(
        requests, sqlite, callback_session_id="ses-callback"
    )
    assert requests.list_pending_callbacks() == [], "not deliverable while running"

    assert _reconcile(service, run_id) == 1

    assert sqlite.get_run(run_id)["callback_status"] == "pending"
    assert [row["id"] for row in requests.list_pending_callbacks()] == [run_id]


def test_teardown_reconcile_is_idempotent(tmp_path):
    """HFR-117: a second pass must be a no-op, not a second settlement.

    Teardowns retry and overlap — an eviction pass, a controller shutdown behind
    it, an End on the same session — so the reconciler runs more than once over the
    same rows as a matter of course. A second write would move ``completed_at`` and
    ``updated_at`` forward, which re-orders the callback drain's queue, and it is
    the shape of write that resets an owed notice's attempt counter and resurrects a
    dead letter.

    Evidence is the WHOLE row before and after, not just the count: a pass that
    returned 0 while still touching ``updated_at`` would satisfy a count-only
    assertion and still have done the damage.
    """

    sqlite, requests, service = _reconcile_fixture(tmp_path)
    run_id = _running_run_on_the_torn_down_session(
        requests, sqlite, callback_session_id="ses-callback"
    )

    assert _reconcile(service, run_id) == 1
    after_first = sqlite.get_run(run_id)

    assert _reconcile(service, run_id) == 0
    assert sqlite.get_run(run_id) == after_first


def test_teardown_reconcile_loses_to_a_terminal_status_that_lands_first(tmp_path):
    """HFR-118: the run's own outcome outranks the teardown's guess about it.

    The reconciler decides what to write from a row it read a moment earlier, and
    pysqlite opens no transaction for a SELECT, so its snapshot really is older than
    its own UPDATE. In that gap the run's turn can finish for real — the eviction
    interrupted the SESSION, not necessarily the work — and an unguarded write would
    replace a genuine ``succeeded`` with a fabricated failure, hand its user an
    interruption notice for a run that completed, and lose the result text.

    So the write is guarded to ``queued|running`` and the reconciler must simply
    lose. The competing write comes from a SECOND store over the same file through
    the production writer, fired between the reconciler's read and its UPDATE, so
    what is raced is the real thing rather than a hand-written row.
    """

    from storage.background import OWED_FAILURE_NOTICE_KEY, SQLiteBackgroundTaskStore

    sqlite, requests, service = _reconcile_fixture(tmp_path)
    run_id = _running_run_on_the_torn_down_session(
        requests, sqlite, callback_session_id="ses-callback"
    )

    fired: list[int] = []

    def _settle_mid_write(conn, cursor, statement, parameters, context, executemany):
        # HFR-355 moved the deferred-intent decision under BEGIN IMMEDIATE. Race the
        # terminal winner immediately BEFORE that lock is acquired: once acquired,
        # the defer read/decide/write unit is intentionally no longer interleavable.
        if fired or not statement.strip().upper().startswith("BEGIN IMMEDIATE"):
            return
        fired.append(1)
        other = SQLiteBackgroundTaskStore(sqlite.db_path)
        try:
            other.settle_run_terminal(
                run_id,
                terminal_status="succeeded",
                result_text="the turn finished after all",
            )
        finally:
            other.close()

    event.listen(sqlite.engine, "before_cursor_execute", _settle_mid_write)
    try:
        settled = _reconcile(service, run_id)
    finally:
        event.remove(sqlite.engine, "before_cursor_execute", _settle_mid_write)

    assert fired, "the interleaved settlement never fired; the race was not exercised"
    assert settled == 0

    saved = sqlite.get_run(run_id)
    assert saved["status"] == "succeeded"
    assert saved["result_text"] == "the turn finished after all"
    assert not (saved.get("metadata") or {}).get("interrupt_reason")
    # A success owes no failure notice, so a stamp here would be a notification
    # about a run that never failed.
    assert (saved.get("metadata") or {}).get(OWED_FAILURE_NOTICE_KEY) is None


@pytest.mark.parametrize("callback_status", ["sent", "skipped"])
def test_teardown_reconcile_preserves_a_finished_callback(tmp_path, callback_status):
    """HFR-119: a callback that is already answered must not be re-opened.

    ``sent`` and ``skipped`` are both FINAL, for opposite reasons — one was
    delivered, one was deliberately declined — and the reconciler has no knowledge
    that could revise either. Resetting the field to ``pending`` would put the row
    back in ``list_pending_callbacks`` and send the same callback a second time; a
    duplicate notification is the failure mode PR6's whole receipt protocol exists
    to prevent, and it would be reintroduced here from the settlement side.

    Both values are exercised, because a writer that special-cases only the one it
    was tested against is the natural way this regresses.
    """

    sqlite, requests, service = _reconcile_fixture(tmp_path)
    run_id = _running_run_on_the_torn_down_session(
        requests, sqlite, callback_session_id="ses-callback"
    )
    requests.update_callback_status(run_id, status=callback_status)
    assert sqlite.get_run(run_id)["callback_status"] == callback_status

    assert _reconcile(service, run_id) == 1

    settled = sqlite.get_run(run_id)
    assert settled["status"] == "failed"
    assert settled["callback_status"] == callback_status
    assert requests.list_pending_callbacks() == []
