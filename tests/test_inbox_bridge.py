from __future__ import annotations

import asyncio

import pytest


def test_inbox_bridge_publishes_controller_bridge_status(monkeypatch):
    from vibe import inbox_bridge

    published = []
    inbox_bridge._bridge_connected = False

    async def stream_events():
        yield "connected", {}
        yield "runs.updated", {"run_id": "run_1", "status": "queued"}

    async def stop_after_disconnect(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox_bridge.internal_client, "stream_events", stream_events)
    monkeypatch.setattr(inbox_bridge.broker, "publish", lambda event_type, data: published.append((event_type, data)))
    monkeypatch.setattr(inbox_bridge.asyncio, "sleep", stop_after_disconnect)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox_bridge.run_inbox_bridge())

    assert inbox_bridge.is_bridge_connected() is False
    # The internal feed's own handshake is consumed, not relayed: the status
    # frame beside it is the browser-facing expression of the same fact, and one
    # fact announced twice made every controller recovery cost two catch-ups.
    assert published == [
        ("workbench.events.bridge.status", {"connected": True}),
        ("runs.updated", {"run_id": "run_1", "status": "queued"}),
        ("workbench.events.bridge.status", {"connected": False}),
    ]


def test_inbox_bridge_tracks_current_status(monkeypatch):
    from vibe import inbox_bridge

    published = []
    inbox_bridge._bridge_connected = False
    monkeypatch.setattr(inbox_bridge.broker, "publish", lambda event_type, data: published.append((event_type, data)))

    inbox_bridge._set_bridge_connected(True)
    assert inbox_bridge.is_bridge_connected() is True

    inbox_bridge._set_bridge_connected(True)
    inbox_bridge._set_bridge_connected(False)

    assert inbox_bridge.is_bridge_connected() is False
    assert published == [
        ("workbench.events.bridge.status", {"connected": True}),
        ("workbench.events.bridge.status", {"connected": False}),
    ]


def test_hfr_173_local_event_echo_is_suppressed_once(monkeypatch):
    from vibe import inbox_bridge

    published = []
    inbox_bridge._bridge_connected = False
    inbox_bridge._local_event_ids.clear()
    inbox_bridge._local_event_order.clear()
    inbox_bridge.remember_local_event("event-1")

    async def stream_events():
        yield "vaults.updated", {"_event_id": "event-1", "scope": "request"}
        yield "vaults.updated", {"_event_id": "event-1", "scope": "request"}

    async def stop_after_disconnect(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(inbox_bridge.internal_client, "stream_events", stream_events)
    monkeypatch.setattr(
        inbox_bridge.broker,
        "publish",
        lambda event_type, data: published.append((event_type, data)),
    )
    monkeypatch.setattr(inbox_bridge.asyncio, "sleep", stop_after_disconnect)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(inbox_bridge.run_inbox_bridge())

    assert published == [
        ("vaults.updated", {"_event_id": "event-1", "scope": "request"}),
    ]


def test_hfr_173_local_event_dedupe_is_bounded():
    from vibe import inbox_bridge

    inbox_bridge._local_event_ids.clear()
    inbox_bridge._local_event_order.clear()
    for index in range(inbox_bridge._LOCAL_EVENT_IDS_MAX + 3):
        inbox_bridge.remember_local_event(f"event-{index}")

    assert len(inbox_bridge._local_event_ids) == inbox_bridge._LOCAL_EVENT_IDS_MAX
    assert "event-0" not in inbox_bridge._local_event_ids
    assert f"event-{inbox_bridge._LOCAL_EVENT_IDS_MAX + 2}" in inbox_bridge._local_event_ids
