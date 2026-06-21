from __future__ import annotations

import asyncio
import threading

from config.v2_config import LarkConfig
from modules.im.feishu import FeishuBot
from modules.im.multi import IMClientRemovalError, MultiIMClient


def test_feishu_stop_disconnects_nested_ws_client(monkeypatch):
    import lark_oapi.ws.client as ws_client_module

    loop = asyncio.new_event_loop()
    disconnected = []

    class _WsClient:
        async def _disconnect(self):
            disconnected.append(True)

    bot = FeishuBot(LarkConfig(app_id="cli_a", app_secret="secret"))
    bot._ws_client = _WsClient()
    bot._loop = loop
    monkeypatch.setattr(ws_client_module, "loop", loop)

    try:
        bot.stop()
    finally:
        loop.close()

    assert disconnected == [True]


def test_feishu_stop_stops_running_lark_ws_loop(monkeypatch):
    import lark_oapi.ws.client as ws_client_module

    loop = asyncio.new_event_loop()
    disconnected = []

    class _WsClient:
        async def _disconnect(self):
            disconnected.append(True)

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    import threading

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    try:
        assert thread.is_alive()
        bot = FeishuBot(LarkConfig(app_id="cli_a", app_secret="secret"))
        bot._ws_client = _WsClient()
        monkeypatch.setattr(ws_client_module, "loop", loop)

        bot.stop()
        thread.join(timeout=2)
    finally:
        if thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
        loop.close()

    assert disconnected == [True]
    assert thread.is_alive() is False


def test_feishu_nested_ws_thread_leak_fails_hot_remove():
    stop_outer = threading.Event()
    stop_leaked_ws = threading.Event()
    leaked_ws = threading.Thread(target=stop_leaked_ws.wait, daemon=True, name="feishu-ws-test")
    leaked_ws.start()
    outer = threading.Thread(target=stop_outer.wait, daemon=True, name="feishu-outer-test")
    outer.start()

    bot = FeishuBot(LarkConfig(app_id="cli_a", app_secret="secret"))
    bot._ws_thread = leaked_ws
    client = MultiIMClient({"lark": bot}, primary_platform="lark")
    client._threads["lark"] = outer

    def _stop_outer_only() -> None:
        stop_outer.set()

    bot.stop = _stop_outer_only

    try:
        try:
            client.remove_client("lark")
        except IMClientRemovalError as exc:
            assert "did not stop all runtime resources" in str(exc)
        else:
            raise AssertionError("remove_client should fail when Feishu nested WS thread leaks")

        assert client.clients["lark"] is bot
        assert client._threads["lark"] is outer
    finally:
        stop_outer.set()
        stop_leaked_ws.set()
        outer.join(timeout=2)
        leaked_ws.join(timeout=2)
