from __future__ import annotations

from core import chat_discovery
from vibe import api


def test_delete_channel_scope_rejects_non_channel_scope(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_delete_scope(platform, native_id, *, scope_type="channel", db_path=None):
        calls.append((platform, native_id, scope_type))
        return {"removed": True, "dismissed": False}

    monkeypatch.setattr(chat_discovery, "delete_scope", fake_delete_scope)

    # Non-channel scope types must be rejected without touching the store.
    result = api.delete_channel_scope("avibe", "p1", scope_type="project")
    assert result["ok"] is False
    assert calls == []


def test_delete_channel_scope_allows_channel(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_delete_scope(platform, native_id, *, scope_type="channel", db_path=None):
        calls.append((platform, native_id, scope_type))
        return {"removed": True, "dismissed": False}

    monkeypatch.setattr(chat_discovery, "delete_scope", fake_delete_scope)

    result = api.delete_channel_scope("discord", "C1")
    assert result == {"ok": True, "removed": True, "dismissed": False}
    assert calls == [("discord", "C1", "channel")]


def test_delete_channel_scope_requires_platform_and_id() -> None:
    assert api.delete_channel_scope("", "C1")["ok"] is False
    assert api.delete_channel_scope("discord", "")["ok"] is False
