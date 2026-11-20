"""Run the Feishu adapter against the fake long connection in a fresh interpreter.

Prints one JSON line: what the adapter received, what it acknowledged, and how
much of ``lark-oapi`` the process ended up importing. Driven by
``tests/test_feishu_sdk_footprint.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from unittest.mock import patch


def main() -> None:
    from tests import network_guard

    sys.addaudithook(network_guard.audit)  # loopback only, as in the pytest process

    from config.v2_config import LarkConfig
    from modules.im.feishu import FeishuBot
    from tests.feishu_fake import FakeFeishu, message_event

    # The hard way to ask for the SDK's client first: from several threads at
    # once, one of them inside a running loop. The SDK takes its own loop from
    # the importing thread, and that loop ends with ``asyncio.run`` here.
    from modules.im.feishu_sdk_ws import sdk_ws_client_module

    complete = []

    def ask() -> None:
        complete.append(hasattr(sdk_ws_client_module(), "Client"))

    async def ask_inside_a_running_loop() -> None:
        go = threading.Event()
        askers = [threading.Thread(target=lambda: (go.wait(), ask())) for _ in range(3)]
        for asker in askers:
            asker.start()
        # This thread asks first and does the import; the others arrive while it is under way.
        go.set()
        ask()
        for asker in askers:
            asker.join()

    asyncio.run(ask_inside_a_running_loop())

    fake = FakeFeishu().start()
    received = []
    try:
        with patch.object(LarkConfig, "api_base_url", property(lambda _self: fake.url)):
            bot = FeishuBot(LarkConfig(app_id="cli_footprint", app_secret="footprint-secret"))
            bot._handle_message_event = received.append  # type: ignore[method-assign]
            thread = threading.Thread(target=bot.run, daemon=True)
            thread.start()
            fake.wait_connected()
            message_id = fake.push_event(message_event(event_id="evt_footprint"))
            acknowledgement = fake.wait_acknowledged(message_id)
            bot.stop()
            thread.join(timeout=10)
    finally:
        fake.close()

    sdk_modules = sorted(name for name in sys.modules if name == "lark_oapi" or name.startswith("lark_oapi."))
    print(
        json.dumps(
            {
                "received_chat": received[0]["message"]["chat_id"] if received else None,
                "acknowledged": acknowledgement.json(),
                "sdk_modules": len(sdk_modules),
                "sdk_api_modules": [name for name in sdk_modules if name.startswith("lark_oapi.api")][:5],
                "sdk_package_loaded": "lark_oapi" in sys.modules,
                "adapter_stopped": not thread.is_alive(),
                "concurrent_loads_complete": complete,
            }
        )
    )


if __name__ == "__main__":
    main()
