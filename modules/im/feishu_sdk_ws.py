"""``lark-oapi``'s long-connection client, loaded without the rest of the SDK.

``import lark_oapi.ws.client`` runs the package ``__init__`` first, which imports
every generated API: about 10,600 modules for a client that needs about 65.
Python only runs that ``__init__`` when ``lark_oapi`` is missing from
``sys.modules``, so a bare package placeholder stands in for it while the client
module is imported, and is taken out again afterwards. The client's one import
from outside its own needs, the SDK's event dispatcher, gets a placeholder too:
our dispatcher (``feishu_events``) takes its place.

Whatever goes wrong, the fallback is the plain import: the same client, at the
full cost. ``tests/test_feishu_sdk_footprint.py`` fails when that happens.

This module goes away with the SDK (``docs/plans/feishu-own-client.md``).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
import threading
import types
from typing import Any

logger = logging.getLogger(__name__)

_CLIENT = "lark_oapi.ws.client"
_lock = threading.Lock()
_loaded: Any = None


def sdk_ws_client_module() -> Any:
    """Return the SDK's ``ws.client`` module (``Client``, ``LogLevel``, its event ``loop``)."""
    global _loaded
    # Under the lock even when it is already loaded: ``sys.modules`` shows a
    # module from the moment its import begins, not from when it is complete.
    with _lock:
        if _loaded is None:
            module = _load()
            # The client runs every connection on one module-level loop, which it
            # takes from whatever thread first imports it: inside a running loop,
            # that is the importer's own. Give it one nobody else runs or closes.
            module.loop = asyncio.new_event_loop()
            _loaded = module
        return _loaded


def _load() -> Any:
    if _CLIENT in sys.modules or "lark_oapi" in sys.modules:
        # The SDK, or this client, is already here or on its way in another
        # thread; the import system waits for it. Nothing to save.
        return importlib.import_module(_CLIENT)
    before = set(sys.modules)
    try:
        return _import_past_package_init()
    except Exception:
        logger.warning(
            "Feishu/Lark adapter: could not load the lark-oapi long-connection client on its own; "
            "importing the whole SDK instead.",
            exc_info=True,
        )
        for name in set(sys.modules) - before:
            if name == "lark_oapi" or name.startswith("lark_oapi."):
                sys.modules.pop(name, None)
        return importlib.import_module(_CLIENT)


def _import_past_package_init() -> Any:
    spec = importlib.util.find_spec("lark_oapi")
    if spec is None or not spec.submodule_search_locations:
        raise ImportError("lark_oapi is not installed as a package")
    root = list(spec.submodule_search_locations)

    package = types.ModuleType("lark_oapi")
    package.__path__ = root
    package.__spec__ = spec
    event_package = types.ModuleType("lark_oapi.event")
    event_package.__path__ = [os.path.join(root[0], "event")]
    dispatcher = types.ModuleType("lark_oapi.event.dispatcher_handler")
    # Only named in the client's signature; any object with the handler method serves.
    dispatcher.EventDispatcherHandler = object
    placeholders = {
        "lark_oapi": package,
        "lark_oapi.event": event_package,
        "lark_oapi.event.dispatcher_handler": dispatcher,
    }

    sys.modules.update(placeholders)
    try:
        return importlib.import_module(_CLIENT)
    finally:
        # Out again, so that a later ``import lark_oapi`` gets the real package
        # and not this shell. The modules the client pulled in stay loaded.
        for name, placeholder in placeholders.items():
            if sys.modules.get(name) is placeholder:
                del sys.modules[name]
