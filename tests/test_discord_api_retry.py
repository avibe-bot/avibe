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
