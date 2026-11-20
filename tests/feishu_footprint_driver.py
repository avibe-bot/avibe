"""Fresh-interpreter checks of how the Feishu adapter loads ``lark-oapi``; prints one JSON line.

``adapter``: run the adapter against the fake long connection and report what it
received, what it acknowledged, and how much of the SDK the process imported.

``beside-an-ordinary-import``: load the adapter's copy of the client while
another thread imports the SDK the ordinary way, and report what each got.

Driven by ``tests/test_feishu_sdk_footprint.py``.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Dict
from unittest.mock import patch


def _modules(package: str) -> list:
    return sorted(name for name in sys.modules if name == package or name.startswith(package + "."))


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

    private = _modules("_avibe_lark_oapi")
    return {
        "received_chat": received[0]["message"]["chat_id"] if received else None,
        "acknowledged": acknowledgement.json(),
        "adapter_stopped": not thread.is_alive() and bot.verify_stopped(),
        "sdk_modules_under_its_own_name": len(_modules("lark_oapi")),
        "sdk_modules_in_our_copy": len(private),
        "api_modules_in_our_copy": [name for name in private if name.startswith("_avibe_lark_oapi.api")][:5],
    }


def beside_an_ordinary_import() -> Dict[str, Any]:
    from modules.im.feishu_sdk_ws import sdk_ws_client_module

    report: Dict[str, Any] = {}
    go = threading.Event()

    def load_our_copy() -> None:
        go.wait()
        report["our_copy_has_client"] = hasattr(sdk_ws_client_module(), "Client")

    def import_the_ordinary_way() -> None:
        go.wait()
        # A submodule first, which holds that module's import lock while the package loads.
        import lark_oapi.ws.client
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

        report["ordinary_import_has_client"] = hasattr(lark_oapi, "Client")
        report["ordinary_import_builds_a_dispatcher"] = type(EventDispatcherHandler.builder("", "").build()).__name__

    threads = [threading.Thread(target=load_our_copy), threading.Thread(target=import_the_ordinary_way)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=60)
    report["both_finished"] = not any(thread.is_alive() for thread in threads)

    import lark_oapi

    ours = sdk_ws_client_module()
    report["the_sdk_s_client_module_is_its_own"] = lark_oapi.ws.client is not ours
    report["the_sdk_s_client_names_the_real_dispatcher"] = (
        lark_oapi.ws.client.EventDispatcherHandler is lark_oapi.EventDispatcherHandler
    )
    report["sdk_modules_under_its_own_name"] = len(_modules("lark_oapi"))
    report["sdk_modules_in_our_copy"] = len(_modules("_avibe_lark_oapi"))
    return report


def without_source_files() -> Dict[str, Any]:
    """An install the copy cannot be made from (here: a client module with no source file)."""
    import importlib.machinery
    import logging

    from modules.im.feishu_sdk_ws import sdk_ws_client_module

    warnings = []
    logging.getLogger("modules.im.feishu_sdk_ws").addFilter(lambda record: warnings.append(record.getMessage()) or True)
    find_spec = importlib.machinery.PathFinder.find_spec

    def sourceless(fullname: str, path: Any = None, target: Any = None) -> Any:
        spec = find_spec(fullname, path, target)
        if spec is not None and fullname.endswith(".ws.client") and fullname.startswith("_avibe_"):
            spec.loader = importlib.machinery.SourcelessFileLoader(fullname, spec.origin)
        return spec

    with patch.object(importlib.machinery.PathFinder, "find_spec", sourceless):
        module = sdk_ws_client_module()
    return {
        "module": module.__name__,
        "warned": any("importing the whole SDK instead" in message for message in warnings),
        "has_client": hasattr(module, "Client"),
        "names_the_real_dispatcher": module.EventDispatcherHandler.__module__,
    }


if __name__ == "__main__":
    from tests import network_guard

    sys.addaudithook(network_guard.audit)  # loopback only, as in the pytest process
    scenario = {
        "adapter": adapter,
        "beside-an-ordinary-import": beside_an_ordinary_import,
        "without-source-files": without_source_files,
    }[sys.argv[1]]
    print(json.dumps(scenario()))
