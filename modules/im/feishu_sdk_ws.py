"""``lark-oapi``'s long-connection client: a private copy, without the rest of the SDK, run safely.

Two things about the SDK are handled here, and only here. Both go away with it
(``docs/plans/feishu-own-client.md``).

**Its size.** ``import lark_oapi.ws.client`` runs the package ``__init__``
first, which imports every generated API: about 10,600 modules for a client
that needs about 65. Python offers no way to import half a package under its
own name that is safe beside other importers of that name. So the client and
what it imports are loaded as a *private copy* under another name,
``_avibe_lark_oapi``: the same files, compiled with the package name replaced,
under a package of ours whose body is empty. The real ``lark_oapi`` is not
touched: not imported, not put into ``sys.modules``, not altered. Code that
imports the SDK the ordinary way gets the SDK, whenever it likes, with every
guarantee the import system gives. The client's one import from outside its
needs, the SDK's event dispatcher (named in a signature), is a module of ours
in that private package.

**Its event loop and its waits.** The SDK runs every client on one module-level
loop, never ends a client's tasks, and asks for its endpoint with a blocking
request that has no timeout. ``SdkLongConnection`` gives each connection a
thread and a loop of its own for its whole life, bounds that request, and ends
every task on the loop when the connection stops, so that a stopped connection
cannot come back.

``tests/test_feishu_sdk_footprint.py`` guards the first, ``tests/test_feishu_events.py``
the second.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REAL = "lark_oapi"
_PRIVATE = "_avibe_lark_oapi"
_PRIVATE_DISPATCHER = f"{_PRIVATE}.event.dispatcher_handler"
_REAL_NAME = re.compile(rf"\b{_REAL}\b")

_load_lock = threading.Lock()
_loaded: Any = None


def sdk_ws_client_module() -> Any:
    """Return our copy of the SDK's ``ws.client`` module (``Client``, ``LogLevel``, ``Frame``)."""
    global _loaded
    # Nothing but this function imports the private names, so holding the lock
    # across the import cannot cross anybody else's import.
    with _load_lock:
        if _loaded is None:
            if not any(isinstance(finder, _PrivateSdkFinder) for finder in sys.meta_path):
                # First in line: the ordinary path finder would find these files too, and load them unrenamed.
                sys.meta_path.insert(0, _PrivateSdkFinder())
            try:
                _loaded = importlib.import_module(f"{_PRIVATE}.ws.client")
            except Exception:
                logger.warning(
                    "Feishu/Lark adapter: could not load the lark-oapi long-connection client on its own; "
                    "importing the whole SDK instead.",
                    exc_info=True,
                )
                _loaded = importlib.import_module(f"{_REAL}.ws.client")
        return _loaded


class _PrivateSdkFinder(importlib.abc.MetaPathFinder):
    """Finds ``_avibe_lark_oapi[.x.y]`` in the files of ``lark_oapi[.x.y]``; knows no other name.

    It stays installed: the SDK's modules import some of their own lazily, at
    run time.
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == _PRIVATE:
            real = importlib.machinery.PathFinder.find_spec(_REAL)  # looks the files up; imports nothing
            if real is None or not real.submodule_search_locations:
                return None
            spec = importlib.machinery.ModuleSpec(_PRIVATE, _OursLoader({}), is_package=True)
            spec.submodule_search_locations = list(real.submodule_search_locations)
            return spec
        if not fullname.startswith(_PRIVATE + "."):
            return None
        if fullname == _PRIVATE_DISPATCHER:
            # Only named in the client's signature; any object with the handler method serves.
            return importlib.machinery.ModuleSpec(fullname, _OursLoader({"EventDispatcherHandler": object}))
        # The import system hands over the parent's search path, and file lookup
        # goes by the last part of the name, which is the same in both packages.
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and isinstance(spec.loader, importlib.machinery.SourceFileLoader) and spec.origin:
            spec.loader = _RenamingLoader(spec.origin)
        return spec


class _OursLoader(importlib.abc.Loader):
    """A module of ours in the private package: the given names and nothing else."""

    def __init__(self, names: dict) -> None:
        self._names = names

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        module.__dict__.update(self._names)


class _RenamingLoader(importlib.abc.Loader):
    """Runs an SDK source file with the package name replaced by the private one.

    Compiled on each start and not cached: a cache would sit beside the SDK's
    own, under the same file name, for different code.
    """

    def __init__(self, path: str) -> None:
        self._path = path

    def create_module(self, spec: Any) -> None:
        return None

    def get_source(self, fullname: str) -> str:
        return _REAL_NAME.sub(_PRIVATE, importlib.util.decode_source(Path(self._path).read_bytes()))

    def exec_module(self, module: Any) -> None:
        code = compile(self.get_source(module.__name__), self._path, "exec", dont_inherit=True)
        exec(code, module.__dict__)


# --- one connection, on a thread and loop of its own ---------------------------------

# The SDK's functions find their loop in a module global, so one process can run
# one of its connections at a time. A second waits here for the first to end.
_slot = threading.Lock()
_SLOT_WAIT_SECONDS = 20.0
# (connect, read) for the endpoint request; together shorter than the wait for the slot.
_ENDPOINT_TIMEOUT = (5.0, 10.0)
_CLOSE_SECONDS = 3.0


class _BoundedRequests:
    """``requests`` as the client module sees it, with a timeout on the endpoint request."""

    def __init__(self, requests: Any) -> None:
        self._requests = requests

    def post(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", _ENDPOINT_TIMEOUT)
        return self._requests.post(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._requests, name)


class SdkLongConnection:
    """One SDK client, started on a thread and loop of its own, and stoppable for good.

    ``event_handler`` is called with each event payload by the SDK's client
    (``_do_without_validation``).

    ``stop`` may be called at any time, from any thread, any number of times,
    before ``start`` included. It only asks: the connection's own thread does
    the ending, once.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str,
        event_handler: Any,
        thread_name: str = "feishu-ws",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain
        self._event_handler = event_handler
        self._stop_requested = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._watcher: Optional[asyncio.Task] = None
        self._thread = threading.Thread(target=self._serve, daemon=True, name=thread_name)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the connection to end and wait for its thread; returns whether the thread is gone."""
        self._stop_requested.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    async def _watch_for_stop(self) -> None:
        """The one place a stop takes effect: cancel everything else on the loop, once."""
        while not self._stop_requested.is_set():
            await asyncio.sleep(0.2)
        this = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not this:
                task.cancel()

    def _serve(self) -> None:
        # One at a time; give up if stopped, or if the one before never ends.
        waited = 0.0
        while not _slot.acquire(timeout=0.1):
            waited += 0.1
            if self._stop_requested.is_set():
                return
            if waited >= _SLOT_WAIT_SECONDS:
                logger.error(
                    "Feishu WS thread not started: another lark-oapi connection is still running in this process"
                )
                return
        loop = asyncio.new_event_loop()
        try:
            # Before the SDK is imported and its client built: the module takes its
            # loop, and the client's fragment cache its clean-up loop, from this thread.
            asyncio.set_event_loop(loop)
            sdk = sdk_ws_client_module()
            sdk.loop = loop
            if sdk.__name__.startswith(_PRIVATE) and not isinstance(sdk.requests, _BoundedRequests):
                sdk.requests = _BoundedRequests(sdk.requests)
            if self._stop_requested.is_set():
                return
            self._loop = loop
            self._client = sdk.Client(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=self._event_handler,
                log_level=sdk.LogLevel.INFO,
                domain=self._domain,
            )
            self._watcher = loop.create_task(self._watch_for_stop())
            logger.info("Feishu WS thread starting, domain=%s", self._domain)
            self._client.start()
        except asyncio.CancelledError:
            pass  # stopped
        except Exception as exc:
            if not self._stop_requested.is_set():
                logger.error("Feishu WS thread crashed: %s", exc, exc_info=True)
        finally:
            try:
                self._close(loop)
            finally:
                _slot.release()

    def _close(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close the socket, end whatever the client left on the loop, and close the loop."""
        client = self._client
        try:
            if self._watcher is not None:
                # From here on nothing cancels but this method: a stop that arrives now only waits.
                self._watcher.cancel()
            if client is not None:
                client._auto_reconnect = False
                connection = getattr(client, "_conn", None)
                if connection is not None:
                    loop.run_until_complete(asyncio.wait_for(connection.close(), _CLOSE_SECONDS))
        except (Exception, asyncio.CancelledError):
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
            except (Exception, asyncio.CancelledError):
                logger.debug("Feishu WS: ending the loop's tasks failed", exc_info=True)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
