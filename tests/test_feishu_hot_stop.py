from __future__ import annotations

import threading

from config.v2_config import LarkConfig
from modules.im.feishu import FeishuBot
from modules.im.multi import IMClientRemovalError, MultiIMClient


class _Connection:
    """A long connection as the adapter sees one: stoppable, and alive or not."""

    def __init__(self, stops: bool) -> None:
        self._stops = stops
        self._alive = True
        self.stop_timeouts: list[float] = []

    def stop(self, timeout: float = 5.0) -> bool:
        self.stop_timeouts.append(timeout)
        if self._stops:
            self._alive = False
        return not self._alive

    def is_alive(self) -> bool:
        return self._alive


def test_feishu_stop_ends_its_long_connection():
    bot = FeishuBot(LarkConfig(app_id="cli_a", app_secret="secret"))
    connection = _Connection(stops=True)
    bot._sdk_connection = connection

    bot.stop()

    assert connection.stop_timeouts == [5.0]
    assert bot.verify_stopped() is True


def test_feishu_long_connection_that_does_not_stop_fails_hot_remove():
    stop_outer = threading.Event()
    outer = threading.Thread(target=stop_outer.wait, daemon=True, name="feishu-outer-test")
    outer.start()

    bot = FeishuBot(LarkConfig(app_id="cli_a", app_secret="secret"))
    bot._sdk_connection = _Connection(stops=False)
    client = MultiIMClient({"lark": bot}, primary_platform="lark")
    client._threads["lark"] = outer
    stop = bot.stop

    def _stop_and_release_outer() -> None:
        stop()
        stop_outer.set()

    bot.stop = _stop_and_release_outer

    try:
        try:
            client.remove_client("lark")
        except IMClientRemovalError as exc:
            assert "did not stop all runtime resources" in str(exc)
        else:
            raise AssertionError("remove_client should fail when the Feishu long connection is still alive")

        assert client.clients["lark"] is bot
        assert client._threads["lark"] is outer
    finally:
        stop_outer.set()
        outer.join(timeout=2)
