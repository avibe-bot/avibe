"""``lark-oapi``'s long-connection client, without the rest of the SDK, and run safely.

Two things about the SDK are handled here, and only here. Both go away with it
(``docs/plans/feishu-own-client.md``).

**Its size.** ``import lark_oapi.ws.client`` runs the package ``__init__``
first, which imports every generated API: about 10,600 modules for a client
that needs about 60. ``sdk_ws_client_module`` puts the package into
``sys.modules`` *unexecuted*, through the import system: a real module object,
made from the package's own spec, whose ``__init__`` runs the first time anybody
asks it for something it does not have yet. Submodules import under it as usual. The client's one
import from outside its own needs, the SDK's event dispatcher, is deferred the
same way. Nobody ever sees a stand-in: code that does ``import lark_oapi`` and
uses it gets the whole SDK, at that moment and at the full cost.

**Its event loop.** The SDK runs every client on one module-level loop and
never ends a client's tasks; a stopped client's reconnect, left on that loop,
comes back when the next client runs it. ``SdkLongConnection`` gives each
connection a loop of its own for its whole life and ends every task on it
when the connection stops.

``tests/test_feishu_sdk_footprint.py`` guards the first, ``tests/test_feishu_events.py``
the second.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import logging
import sys
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_PACKAGE = "lark_oapi"
_DISPATCHER = "lark_oapi.event.dispatcher_handler"
_CLIENT = "lark_oapi.ws.client"

# One lock for loading the client and for running a deferred body: the two take
# the import system's per-module locks in opposite orders, and must not overlap.
_load_lock = threading.RLock()
_loaded: Any = None
# On the thread that loads the client: ``loading`` for the whole of it,
# ``active`` while the client module itself is being imported.
_importing_client = threading.local()


def sdk_ws_client_module() -> Any:
    """Return the SDK's ``ws.client`` module (``Client``, ``LogLevel``, ``Frame``)."""
    global _loaded
    with _load_lock:
        if _loaded is None:
            _loaded = _load()
        return _loaded


def _load() -> Any:
    finder = _DeferringFinder()
    sys.meta_path.insert(0, finder)
    _importing_client.loading = True
    try:
        # Through the import system, so that its own locks decide between this
        # and any other thread's ``import lark_oapi``: there is one package
        # object, deferred if we came first and whole if they did.
        importlib.import_module(_PACKAGE)
        importlib.import_module(_DISPATCHER)
        _importing_client.active = True
        try:
            return importlib.import_module(_CLIENT)
        finally:
            _importing_client.active = False
    except Exception:
        logger.warning(
            "Feishu/Lark adapter: could not load the lark-oapi long-connection client on its own; "
            "importing the whole SDK instead.",
            exc_info=True,
        )
        # Make whatever was deferred real, in place, and import the client the ordinary way.
        for name in (_DISPATCHER, _PACKAGE):
            complete = getattr(sys.modules.get(name), "_avibe_complete", None)
            if complete is not None:
                complete()
        return importlib.import_module(_CLIENT)
    finally:
        _importing_client.loading = False
        sys.meta_path.remove(finder)


class _DeferringFinder(importlib.abc.MetaPathFinder):
    """Finds the SDK's package and its dispatcher module as usual, and loads them without running their bodies."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        # Only for the thread that loads the client: an ordinary import that is
        # under way on another thread stays ordinary to its end.
        if fullname not in (_PACKAGE, _DISPATCHER) or not getattr(_importing_client, "loading", False):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _DeferringLoader(spec.loader)
        return spec


class _DeferringLoader(importlib.abc.Loader):
    """Leaves a module unexecuted; its body runs on the first attribute it lacks.

    The module object is the real one, made from its own spec, and it stays:
    what is imported under it, before or after its body runs, is bound to it.
    Nobody is handed a stand-in for anything, with one exception: the client
    module's own import, which names the dispatcher class in a signature, gets
    ``object`` for it.
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    def create_module(self, spec: Any) -> Any:
        return self._real.create_module(spec)

    def exec_module(self, module: Any) -> None:
        real = self._real
        name = module.__name__
        state = {"done": False, "running_on": None}

        def complete() -> None:
            with _load_lock:
                if state["done"]:
                    return
                state["running_on"] = threading.get_ident()
                try:
                    real.exec_module(module)
                finally:
                    state["running_on"] = None
                state["done"] = True
                for leftover in ("__getattr__", "_avibe_complete"):
                    module.__dict__.pop(leftover, None)
                module.__loader__ = real
                module.__spec__.loader = real

        def __getattr__(attribute: str) -> Any:  # PEP 562: asked only for what the module lacks
            if state["running_on"] == threading.get_ident() or (
                attribute.startswith("__") and attribute.endswith("__") and attribute != "__all__"
            ):
                # The body itself probing for a name not bound yet, or the import system
                # asking what kind of module this is (``__path__``): neither is a use of it.
                raise AttributeError(f"module {name!r} has no attribute {attribute!r}")
            if (
                name == _DISPATCHER
                and attribute == "EventDispatcherHandler"
                and getattr(_importing_client, "active", False)
            ):
                return object
            complete()
            try:
                return module.__dict__[attribute]
            except KeyError:
                raise AttributeError(f"module {name!r} has no attribute {attribute!r}") from None

        module.__getattr__ = __getattr__
        module._avibe_complete = complete


# --- one connection, on a loop of its own -------------------------------------------

# The SDK's functions find their loop in a module global, so one process can run
# one of its connections at a time. A second waits here for the first to end.
_slot = threading.Lock()
_SLOT_WAIT_SECONDS = 10.0


class SdkLongConnection:
    """One ``lark_oapi.ws.Client``, started on a thread and loop of its own, and stoppable for good.

    ``event_handler`` is called with each event payload by the SDK's client
    (``_do_without_validation``). ``on_client`` is called with the SDK module
    and the client once it exists, before it connects.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
        event_handler: Any,
        thread_name: str = "feishu-ws",
        on_client: Optional[Callable[[Any, Any], None]] = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._event_handler = event_handler
        self._on_client = on_client
        self._stopping = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._thread = threading.Thread(target=self._serve, daemon=True, name=thread_name)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> bool:
        """End the connection, its tasks and its thread; returns whether the thread is gone."""
        self._stopping = True
        client, loop = self._client, self._loop
        if client is not None:
            client._auto_reconnect = False
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._cancel_all, loop)
            except RuntimeError:
                pass  # the loop closed between the check and the call
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    @staticmethod
    def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    async def _watch_for_stop(self) -> None:
        """End everything on the loop once ``stop`` was called, however early it was."""
        while not self._stopping:
            await asyncio.sleep(0.2)
        loop = asyncio.get_running_loop()
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    def _serve(self) -> None:
        # One at a time; give up quietly if stopped while waiting for the slot.
        waited = 0.0
        while not _slot.acquire(timeout=0.1):
            waited += 0.1
            if self._stopping:
                return
            if waited >= _SLOT_WAIT_SECONDS:
                logger.error(
                    "Feishu WS thread not started: another lark-oapi connection is still running in this process"
                )
                return
        loop: Optional[asyncio.AbstractEventLoop] = None
        try:
            if self._stopping:
                return
            sdk = sdk_ws_client_module()
            loop = asyncio.new_event_loop()
            # The client's fragment cache schedules its clean-up on the thread's
            # current loop, and its functions use the module's: make both this one.
            asyncio.set_event_loop(loop)
            sdk.loop = loop
            self._loop = loop
            self._client = sdk.Client(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=self._event_handler,
                log_level=sdk.LogLevel.INFO,
                domain=self._domain,
            )
            if self._on_client is not None:
                self._on_client(sdk, self._client)
            loop.create_task(self._watch_for_stop())
            logger.info("Feishu WS thread starting, domain=%s", self._domain)
            self._client.start()
        except asyncio.CancelledError:
            pass  # stopped
        except Exception as exc:
            if not self._stopping:
                logger.error("Feishu WS thread crashed: %s", exc, exc_info=True)
        finally:
            try:
                if loop is not None:
                    self._close(loop)
            finally:
                _slot.release()

    def _close(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close the socket, end whatever the client left on the loop, and close the loop."""
        self._stopping = True
        client = self._client
        try:
            if client is not None:
                # No reconnect from here on, then the socket, then every task still there.
                client._auto_reconnect = False
                connection = getattr(client, "_conn", None)
                if connection is not None:
                    loop.run_until_complete(asyncio.wait_for(connection.close(), 3.0))
        except Exception:
            logger.debug("Feishu WS: closing the connection failed", exc_info=True)
        finally:
            try:
                tasks = list(asyncio.all_tasks(loop))
                for task in tasks:
                    task.cancel()
                if tasks:
                    # Their exceptions are read here, so that none is reported as never retrieved.
                    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.debug("Feishu WS: ending the loop's tasks failed", exc_info=True)
            asyncio.set_event_loop(None)
            loop.close()
