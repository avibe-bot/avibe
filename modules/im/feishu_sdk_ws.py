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
thread and a loop of its own for its whole life and makes a stop final at the
two points where a stopped connection could still do harm: the endpoint
request, without which no connection opens, and the hand-over of an event.

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


# A private package's search path is a real directory behind this marker: a
# finder other than ours cannot read it, and so cannot load these files unrenamed.
_PATH_MARK = "avibe-private-sdk:"


class _PrivateSdkFinder(importlib.abc.MetaPathFinder):
    """Finds ``_avibe_lark_oapi[.x.y]`` in the files of ``lark_oapi[.x.y]``; knows no other name.

    It stays installed: the SDK's modules import some of their own lazily, at
    run time. A module it cannot rename (no source file: a zip, a bare
    ``.pyc``, an extension) is an ``ImportError``, never the unrenamed module.
    """

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == _PRIVATE:
            real = importlib.machinery.PathFinder.find_spec(_REAL)  # looks the files up; imports nothing
            if real is None or not real.submodule_search_locations:
                return None
            spec = importlib.machinery.ModuleSpec(_PRIVATE, _OursLoader({}), is_package=True)
            spec.submodule_search_locations = [_PATH_MARK + entry for entry in real.submodule_search_locations]
            return spec
        if not fullname.startswith(_PRIVATE + "."):
            return None
        if fullname == _PRIVATE_DISPATCHER:
            # Only named in the client's signature; any object with the handler method serves.
            return importlib.machinery.ModuleSpec(fullname, _OursLoader({"EventDispatcherHandler": object}))
        # File lookup goes by the last part of the name, which is the same in both packages.
        directories = [entry[len(_PATH_MARK) :] for entry in path or () if entry.startswith(_PATH_MARK)]
        spec = importlib.machinery.PathFinder.find_spec(fullname, directories)
        if spec is None:
            return None
        if not isinstance(spec.loader, importlib.machinery.SourceFileLoader) or not spec.origin:
            raise ImportError(f"{fullname} has no source file to load under a private name", name=fullname)
        spec.loader = _RenamingLoader(spec.origin)
        if spec.submodule_search_locations is not None:
            spec.submodule_search_locations = [_PATH_MARK + entry for entry in spec.submodule_search_locations]
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
# The endpoint request: (connect, read) for the socket, and a deadline for the
# whole of it, shorter than the wait for the slot.
_ENDPOINT_TIMEOUT = (5.0, 10.0)
_ENDPOINT_DEADLINE_SECONDS = 15.0
_CLOSE_SECONDS = 3.0
_STOP_TICK_SECONDS = 0.1


class _Stopped(Exception):
    """Raised to an event the SDK's client hands over after its connection was asked to stop."""


class _EndpointRequests:
    """``requests`` as the client module sees it, for the one request it makes: for its endpoint.

    Every connection the client opens starts with that request, so this is
    where a stopped connection is kept from opening another. The SDK sends it
    from the loop's thread with no timeout; here it runs beside that thread,
    under a deadline, and gives way to a stop at once.
    """

    def __init__(self, requests: Any, owner: "SdkLongConnection") -> None:
        self._requests = requests
        self._owner = owner

    def post(self, url: str, **kwargs: Any) -> Any:
        stop = self._owner._stop_requested
        if stop.is_set():
            raise asyncio.CancelledError()
        kwargs.setdefault("timeout", _ENDPOINT_TIMEOUT)
        outcome: dict = {}

        def send() -> None:
            try:
                outcome["reply"] = self._requests.post(url, **kwargs)
            except BaseException as exc:
                outcome["error"] = exc

        sender = threading.Thread(target=send, daemon=True, name="feishu-ws-endpoint")
        sender.start()
        waited = 0.0
        while sender.is_alive():
            sender.join(0.05)
            waited += 0.05
            if stop.is_set():
                raise asyncio.CancelledError()
            if waited >= _ENDPOINT_DEADLINE_SECONDS and sender.is_alive():
                raise TimeoutError(f"no endpoint within {_ENDPOINT_DEADLINE_SECONDS:g} s")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["reply"]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._requests, name)


class _UntilStopped:
    """The event handler as the SDK's client sees it: events are handled until the connection is asked to stop.

    One that arrives after that is refused, so that Feishu sends it again and
    the connection that replaces this one gets it.
    """

    def __init__(self, handler: Any, owner: "SdkLongConnection") -> None:
        self._handler = handler
        self._owner = owner

    def _do_without_validation(self, payload: bytes) -> Any:
        if self._owner._stop_requested.is_set():
            raise _Stopped("the connection is stopping")
        return self._handler._do_without_validation(payload)

    do_without_validation = _do_without_validation


class SdkLongConnection:
    """One SDK client, started on a thread and loop of its own, and stoppable for good.

    ``event_handler`` is called with each event payload by the SDK's client
    (``_do_without_validation``).

    ``stop`` may be called at any time, from any thread, any number of times,
    before ``start`` included. It only asks. From that moment the client can
    open no connection and no event is taken; the connection's own thread then
    ends what is left, once.
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
        """Once a stop is asked for, keep cancelling whatever the client runs, until its ``start`` has unwound.

        The client's ``start`` runs one thing after another on the loop; a single
        sweep would end the one that is running and miss the next.
        """
        while True:
            if self._stop_requested.is_set():
                this = asyncio.current_task()
                for task in asyncio.all_tasks():
                    if task is not this:
                        task.cancel()
            await asyncio.sleep(_STOP_TICK_SECONDS)

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
        try:
            self._run_client()
        except Exception as exc:
            logger.error("Feishu WS thread could not run: %s", exc, exc_info=True)
        finally:
            _slot.release()

    def _run_client(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            # Before the SDK is imported and its client built: the module takes its
            # loop, and the client's fragment cache its clean-up loop, from this thread.
            asyncio.set_event_loop(loop)
            sdk = sdk_ws_client_module()
            sdk.loop = loop
            sdk.requests = _EndpointRequests(getattr(sdk.requests, "_requests", sdk.requests), self)
            if self._stop_requested.is_set():
                return
            self._loop = loop
            self._client = sdk.Client(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=_UntilStopped(self._event_handler, self),
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
            self._close(loop)

    def _close(self, loop: asyncio.AbstractEventLoop) -> None:
        """Close the socket, end whatever the client left on the loop, and close the loop."""
        client = self._client
        # Whatever ended the client, nothing of it may go on: no connection, no event.
        self._stop_requested.set()
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
