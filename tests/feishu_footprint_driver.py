"""Fresh-interpreter checks of how the Feishu adapter loads ``lark-oapi``; prints one JSON line.

``adapter``: run the adapter against the fake long connection and report what it
received, what it acknowledged, and how much of the SDK the process imported.

``beside-an-ordinary-import``: load the client on its own while another thread
imports and uses the SDK the ordinary way, and report what each of them got.

Driven by ``tests/test_feishu_sdk_footprint.py``.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict
from unittest.mock import patch


def _sdk_modules() -> list:
    return sorted(name for name in sys.modules if name == "lark_oapi" or name.startswith("lark_oapi."))


def adapter() -> Dict[str, Any]:
    from config.v2_config import LarkConfig
    from modules.im.feishu import FeishuBot
    from tests.feishu_fake import FakeFeishu, message_event

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

    modules = _sdk_modules()
    return {
        "received_chat": received[0]["message"]["chat_id"] if received else None,
        "acknowledged": acknowledgement.json(),
        "sdk_modules": len(modules),
        "sdk_api_modules": [name for name in modules if name.startswith("lark_oapi.api")][:5],
        "adapter_stopped": not thread.is_alive() and bot.verify_stopped(),
    }


def beside_an_ordinary_import() -> Dict[str, Any]:
    from modules.im.feishu_sdk_ws import sdk_ws_client_module

    report: Dict[str, Any] = {}
    go = threading.Event()

    def load_the_client_alone() -> None:
        go.wait()
        report["client_alone_has_client"] = hasattr(sdk_ws_client_module(), "Client")

    def import_the_ordinary_way() -> None:
        go.wait()
        import lark_oapi

        # The reference is used as it was handed over, as any caller would.
        report["ordinary_import_has_client"] = hasattr(lark_oapi, "Client")
        report["ordinary_import_builds_a_dispatcher"] = type(
            lark_oapi.EventDispatcherHandler.builder("", "").build()
        ).__name__

    threads = [threading.Thread(target=load_the_client_alone), threading.Thread(target=import_the_ordinary_way)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=60)

    import lark_oapi
    import lark_oapi.core
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

    report["package_knows_its_children"] = all(
        getattr(lark_oapi, child, None) is sys.modules[f"lark_oapi.{child}"] for child in ("core", "ws", "api")
    )
    report["dispatcher_is_the_real_class"] = EventDispatcherHandler is lark_oapi.EventDispatcherHandler
    report["client_module_is_the_package_s"] = sdk_ws_client_module() is lark_oapi.ws.client
    report["sdk_modules"] = len(_sdk_modules())
    return report


def alone_then_ordinary() -> Dict[str, Any]:
    from modules.im.feishu_sdk_ws import sdk_ws_client_module

    client_module = sdk_ws_client_module()
    report: Dict[str, Any] = {"sdk_modules_after_the_client_alone": len(_sdk_modules())}

    import lark_oapi.core  # a submodule first: its parent must know it

    report["package_knows_core"] = lark_oapi.core is sys.modules["lark_oapi.core"]
    report["sdk_modules_after_a_submodule"] = len(_sdk_modules())
    report["has_client"] = hasattr(lark_oapi, "Client")  # the first thing the package lacks: the whole SDK arrives
    report["sdk_modules_after_using_the_package"] = len(_sdk_modules())
    report["client_module_is_the_package_s"] = client_module is lark_oapi.ws.client
    return report


if __name__ == "__main__":
    from tests import network_guard

    sys.addaudithook(network_guard.audit)  # loopback only, as in the pytest process
    scenario = {
        "adapter": adapter,
        "beside-an-ordinary-import": beside_an_ordinary_import,
        "alone-then-ordinary": alone_then_ordinary,
    }[sys.argv[1]]
    print(json.dumps(scenario()))
