from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import DiscordConfig
from modules.im.discord import DiscordBot


def test_discord_runtime_retries_startup_failure_with_backoff(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    waits: list[float] = []
    attempts: list[FakeDiscordClient] = []
    clients: list[FakeDiscordClient] = []

    class FakeDiscordClient:
        def __init__(self) -> None:
            self.http = SimpleNamespace(connector=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def start(self, _token: str) -> None:
            attempts.append(self)
            if len(attempts) < 3:
                raise ConnectionResetError("discord unavailable")
            bot._stop_event.set()

    def new_client() -> FakeDiscordClient:
        client = FakeDiscordClient()
        clients.append(client)
        return client

    monkeypatch.setattr(bot, "_new_client", new_client)
    bot.client = new_client()

    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda _configured: None)
    monkeypatch.setattr(bot._stop_event, "wait", lambda delay: waits.append(delay) or False)

    bot.run()

    assert len(attempts) == 3
    assert attempts == clients
    assert waits == [1.0, 2.0]


def test_discord_runtime_stop_interrupts_retry_wait(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    new_client = Mock()

    def fake_asyncio_run(coro) -> None:
        coro.close()
        raise ConnectionResetError("discord unavailable")

    def stop_during_wait(_delay: float) -> bool:
        bot.stop()
        return bot._stop_event.is_set()

    monkeypatch.setattr("modules.im.discord.asyncio.run", fake_asyncio_run)
    monkeypatch.setattr(bot._stop_event, "wait", stop_during_wait)
    monkeypatch.setattr(bot, "_new_client", new_client)

    bot.run()

    new_client.assert_not_called()


def test_discord_runtime_preserves_stop_requested_before_thread_start(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    asyncio_run = Mock(side_effect=AssertionError("stopped runtime must not start"))
    monkeypatch.setattr("modules.im.discord.asyncio.run", asyncio_run)

    bot.stop()
    bot.run()

    asyncio_run.assert_not_called()


def test_discord_runtime_replaces_client_after_proxy_setup_failure(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    clients: list[FakeDiscordClient] = []
    starts: list[FakeDiscordClient] = []
    proxy_urls = iter(["socks5://bad proxy", None])

    class FakeDiscordClient:
        def __init__(self) -> None:
            self.http = SimpleNamespace(connector=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def start(self, _token: str) -> None:
            starts.append(self)
            bot._stop_event.set()

    def new_client() -> FakeDiscordClient:
        client = FakeDiscordClient()
        clients.append(client)
        return client

    monkeypatch.setattr(bot, "_new_client", new_client)
    bot.client = new_client()
    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda _configured: next(proxy_urls))
    monkeypatch.setattr(
        "aiohttp_socks.ProxyConnector.from_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid proxy URL")),
    )
    monkeypatch.setattr(bot._stop_event, "wait", lambda _delay: False)

    bot.run()

    assert len(clients) == 2
    assert starts == [clients[1]]


def test_discord_runtime_rechecks_stop_before_client_start(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    starts: list[bool] = []

    class FakeDiscordClient:
        def __init__(self) -> None:
            self.http = SimpleNamespace(connector=None)

        async def __aenter__(self):
            bot.stop()
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def start(self, _token: str) -> None:
            starts.append(True)

        async def close(self) -> None:
            return None

    bot.client = FakeDiscordClient()
    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda _configured: None)

    bot.run()

    assert starts == []


def test_discord_runtime_reports_unready_before_replacing_client(monkeypatch) -> None:
    bot = DiscordBot(DiscordConfig(bot_token="test-token"))
    events: list[str] = []
    attempts = 0

    class FakeDiscordClient:
        def __init__(self, name: str) -> None:
            self.name = name
            self.http = SimpleNamespace(connector=None)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def start(self, _token: str) -> None:
            nonlocal attempts
            attempts += 1
            events.append(f"start:{self.name}")
            if attempts == 1:
                raise ConnectionResetError("discord unavailable")
            bot._stop_event.set()

    async def on_transport_unready() -> None:
        events.append("unready")

    def new_client() -> FakeDiscordClient:
        client = FakeDiscordClient(f"client-{attempts + 1}")
        events.append(f"new:{client.name}")
        return client

    bot.register_callbacks(on_transport_unready=on_transport_unready)
    monkeypatch.setattr(bot, "_new_client", new_client)
    bot.client = new_client()
    monkeypatch.setattr("vibe.proxy.resolve_proxy", lambda _configured: None)
    monkeypatch.setattr(bot._stop_event, "wait", lambda _delay: False)

    bot.run()

    assert events == [
        "new:client-1",
        "start:client-1",
        "unready",
        "new:client-2",
        "start:client-2",
        "unready",
    ]
