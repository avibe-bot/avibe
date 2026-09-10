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
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.inbox_events import InboxEventBus
from storage.background import EXECUTION_RUN_TYPES, RUN_STATUS_ALIASES, TERMINAL_RUN_STATUSES


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


@pytest.mark.parametrize("started_at", [None, "2026-07-04T00:00:01+00:00"])
@pytest.mark.parametrize("completed_at", [None, "2026-07-04T00:00:02+00:00"])
def test_run_updated_publisher_preserves_available_timestamps(monkeypatch, started_at, completed_at):
    from core import inbox_events

    published = []
    monkeypatch.setattr(inbox_events.bus, "publish", lambda *event: published.append(event))
    inbox_events.publish_run_updated(
        run_id="run_stamps",
        status="succeeded",
        started_at=started_at,
        completed_at=completed_at,
    )

    expected = {"run_id": "run_stamps", "status": "succeeded"}
    expected.update(
        (key, value)
        for key, value in {"started_at": started_at, "completed_at": completed_at}.items()
        if value is not None
    )
    assert published == [(inbox_events.RUNS_UPDATED_EVENT, expected)]


@pytest.mark.parametrize("stored_status, status", RUN_STATUS_ALIASES.items())
@pytest.mark.parametrize("started_at", [None, "2026-07-04T00:00:01+00:00"])
@pytest.mark.parametrize("completed_at", [None, "2026-07-04T00:00:02+00:00"])
def test_run_row_events_only_add_available_terminal_timestamps(
    monkeypatch, stored_status, status, started_at, completed_at
):
    from core import inbox_events
    from storage.background import _publish_run_rows_updated

    published = []
    bridged = []
    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", False)
    monkeypatch.setattr(inbox_events.bus, "publish", lambda *event: published.append(event))
    monkeypatch.setattr(
        "vibe.internal_client.publish_event_sync",
        lambda event_type, data, **kwargs: bridged.append((event_type, data)),
    )
    _publish_run_rows_updated(
        [{"id": "run_stamps", "status": stored_status, "started_at": started_at, "completed_at": completed_at}]
    )

    expected = {"run_id": "run_stamps", "status": status, "cancel_requested": False}
    if status in TERMINAL_RUN_STATUSES:
        expected.update(
            (key, value)
            for key, value in {"started_at": started_at, "completed_at": completed_at}.items()
            if value is not None
        )
    assert published == [(inbox_events.RUNS_UPDATED_EVENT, expected)]
    assert bridged == published


@pytest.mark.parametrize("terminal_status", sorted(TERMINAL_RUN_STATUSES))
@pytest.mark.parametrize("run_type", sorted(EXECUTION_RUN_TYPES | {"watch_runtime", "future_run_type"}))
def test_sqlite_background_store_publishes_run_updates(tmp_path, run_type, terminal_status):
    async def scenario():
        from core import inbox_events
        from storage.background import SQLiteBackgroundTaskStore

        sub_id, queue = inbox_events.bus.subscribe()
        store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
        try:
            store.enqueue_run(
                {
                    "id": "run_evt_1",
                    "request_type": run_type,
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
                status=terminal_status,
                updated_at="2026-07-04T00:01:02+00:00",
                completed_at="2026-07-04T00:00:02+00:00",
            )
            terminal = await asyncio.wait_for(queue.get(), timeout=1.0)
            return queued, running, terminal, store.get_run("run_evt_1")
        finally:
            store.close()
            inbox_events.bus.unsubscribe(sub_id)

    queued, running, terminal, persisted = asyncio.run(scenario())
    assert queued == (
        "runs.updated",
        {
            "run_id": "run_evt_1",
            "status": "queued",
            "run_type": run_type,
            "session_id": "ses_evt",
            "updated_at": "2026-07-04T00:00:00+00:00",
            "cancel_requested": False,
        },
    )
    assert running == (
        "runs.updated",
        {**queued[1], "status": "running", "updated_at": "2026-07-04T00:00:01+00:00"},
    )
    assert persisted["started_at"] == "2026-07-04T00:00:01+00:00"
    assert persisted["completed_at"] == "2026-07-04T00:00:02+00:00"
    assert terminal == (
        "runs.updated",
        {
            **queued[1],
            "status": terminal_status,
            "started_at": persisted["started_at"],
            "completed_at": persisted["completed_at"],
            "updated_at": "2026-07-04T00:01:02+00:00",
        },
    )


def test_hfr_156_sqlite_commit_bridges_run_update_to_controller(tmp_path, monkeypatch):
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


def test_hfr_156_bridge_failure_does_not_rollback_committed_run(tmp_path, monkeypatch):
    from core import inbox_events
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setattr(inbox_events, "_CONTROLLER_PROCESS", False)

    def _fail_bridge(*_args, **_kwargs):
        raise OSError("dispatch socket unavailable")

    monkeypatch.setattr("vibe.internal_client.publish_event_sync", _fail_bridge)
    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        store.enqueue_run(
            {
                "id": "run_evt_bridge_failure",
                "request_type": "agent_run",
                "status": "queued",
                "message": "hello",
                "created_at": "2026-07-04T00:00:00+00:00",
                "updated_at": "2026-07-04T00:00:00+00:00",
            }
        )
        persisted = store.get_run("run_evt_bridge_failure")
    finally:
        store.close()

    assert persisted is not None
    assert persisted["status"] == "queued"


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
