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
    assert published == [
        ("workbench.events.bridge.status", {"connected": True}),
        ("connected", {}),
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
