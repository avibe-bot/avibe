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
