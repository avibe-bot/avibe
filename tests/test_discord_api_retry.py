from __future__ import annotations

import urllib.error
import urllib.request

from vibe import api


class _FakeResp:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._payload.encode("utf-8")


def test_discord_api_get_retries_on_429_then_succeeds(monkeypatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    class _FakeOpener:
        def open(self, _req, timeout=10):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "https://discord.com", 429, "Too Many Requests", {"Retry-After": "0"}, None
                )
            return _FakeResp('{"ok": true}')

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener())
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    result = api._discord_api_get("bot-token", "guilds/1/channels")

    assert result == {"ok": True}
    assert calls["n"] == 2  # one 429, one success
    assert sleeps  # backoff slept at least once


def test_lark_list_chats_live_marks_truncated_on_repeated_page_token(monkeypatch) -> None:
    import json as _json

    pages = [
        {"code": 0, "data": {"items": [{"chat_id": "oc_1", "name": "one"}], "has_more": True, "page_token": "P1"}},
        # Server repeats the same cursor while still claiming more pages.
        {"code": 0, "data": {"items": [{"chat_id": "oc_2", "name": "two"}], "has_more": True, "page_token": "P1"}},
    ]
    idx = {"n": 0}

    def fake_urlopen(_req, timeout=10):
        payload = pages[min(idx["n"], len(pages) - 1)]
        idx["n"] += 1
        return _FakeResp(_json.dumps(payload))

    monkeypatch.setattr(api, "_lark_tenant_token", lambda *a, **k: "tok")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = api.lark_list_chats_live("app", "secret", "feishu")

    assert result["ok"] is True
    assert result["truncated"] is True
    assert {c["id"] for c in result["channels"]} == {"oc_1", "oc_2"}


def test_lark_list_chats_live_not_truncated_when_complete(monkeypatch) -> None:
    import json as _json

    page = {"code": 0, "data": {"items": [{"chat_id": "oc_1", "name": "one"}], "has_more": False, "page_token": ""}}

    monkeypatch.setattr(api, "_lark_tenant_token", lambda *a, **k: "tok")
    monkeypatch.setattr(urllib.request, "urlopen", lambda _req, timeout=10: _FakeResp(_json.dumps(page)))

    result = api.lark_list_chats_live("app", "secret", "feishu")

    assert result["ok"] is True
    assert result["truncated"] is False
    assert [c["id"] for c in result["channels"]] == ["oc_1"]


def test_discord_api_get_raises_on_non_retryable(monkeypatch) -> None:
    class _FakeOpener:
        def open(self, _req, timeout=10):
            raise urllib.error.HTTPError(
                "https://discord.com", 401, "Unauthorized", {}, None
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _FakeOpener())

    try:
        api._discord_api_get("bot-token", "guilds/1/channels")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:  # pragma: no cover
        raise AssertionError("expected HTTPError for 401")
