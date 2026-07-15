from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import DiscordConfig
from modules.im.discord import DiscordBot


def test_discord_runtime_retries_startup_failure_with_backoff(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    waits: list[float] = []

    class FakeDiscordClient:
        def __init__(self) -> None:
            self.attempts = 0
            self.clear = Mock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def start(self, _token: str) -> None:
            self.attempts += 1
            if self.attempts < 3:
                raise ConnectionResetError("discord unavailable")
            bot._stop_event.set()

    client = FakeDiscordClient()
    bot.client = client

    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda _configured: None)
    monkeypatch.setattr(bot._stop_event, "wait", lambda delay: waits.append(delay) or False)

    bot.run()

    assert client.attempts == 3
    assert waits == [1.0, 2.0]
    assert client.clear.call_count == 2


def test_discord_runtime_stop_interrupts_retry_wait(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    clear = Mock()

    def fake_asyncio_run(coro) -> None:
        coro.close()
        raise ConnectionResetError("discord unavailable")

    def stop_during_wait(_delay: float) -> bool:
        bot.stop()
        return bot._stop_event.is_set()

    monkeypatch.setattr("modules.im.discord.asyncio.run", fake_asyncio_run)
    monkeypatch.setattr(bot._stop_event, "wait", stop_during_wait)
    monkeypatch.setattr(bot.client, "clear", clear)

    bot.run()

    clear.assert_not_called()
