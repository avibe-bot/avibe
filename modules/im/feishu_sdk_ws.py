"""``lark-oapi``'s long-connection client, run so that it can be stopped.

The SDK's client was not written to be stopped: one module-level event loop
for every client, tasks that never end, and a ``start`` that runs one step
after another. A client "stopped" on that shared loop kept its reconnect
there, and the next client started in the same process ran it: the old one
came back as a second connection for the same app and took a share of its
events.

``SdkLongConnection`` gives each connection a thread and a loop of its own for
its whole life, and makes a stop final at the two points where a stopped
client could still do harm: asking for an endpoint, which every new
connection begins with, and handing over an event.

It goes away with the SDK (``docs/plans/feishu-own-client.md``).
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The SDK's functions find their loop in a module global, so one process can run
# one of its connections at a time. A second waits here for the first to end.
_slot = threading.Lock()
_SLOT_WAIT_SECONDS = 20.0
_CLOSE_SECONDS = 3.0
_STOP_TICK_SECONDS = 0.1


class _Stopped(Exception):
    """Raised to an event the SDK's client hands over after its connection was asked to stop."""


class _UntilStopped:
    """The event handler as the SDK's client sees it: events are handled until the connection is asked to stop.

    One that arrives after that is refused, so that Feishu sends it again and
    the connection that replaces this one gets it.
    """

    def __init__(self, handler: Any, stop_requested: threading.Event) -> None:
        self._handler = handler
        self._stop_requested = stop_requested

    def _do_without_validation(self, payload: bytes) -> Any:
        if self._stop_requested.is_set():
            raise _Stopped("the connection is stopping")
        return self._handler._do_without_validation(payload)

    do_without_validation = _do_without_validation


def _stoppable_client_class(sdk: Any, stop_requested: threading.Event) -> type:
    """The SDK's ``Client``, unable to connect once ``stop_requested`` is set."""

    class StoppableClient(sdk.Client):
        def _get_conn_url(self) -> str:
            # Every connection, first or again, begins by asking for an endpoint.
            if stop_requested.is_set():
                raise asyncio.CancelledError()
            return super()._get_conn_url()

    return StoppableClient


class SdkLongConnection:
    """One SDK client, started on a thread and loop of its own, and stoppable for good.

    ``event_handler`` is called with each event payload by the SDK's client
    (``_do_without_validation``).

    ``stop`` may be called at any time, from any thread, any number of times,
    before ``start`` included. It only asks. From that moment the client is
    given no endpoint, so it can begin no connection, and no event is taken
    from it; the connection's own thread then ends what is left, once.

    Not changed from the SDK: its endpoint request blocks the thread and has no
    timeout. While one is outstanding, the thread cannot end.
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
            sdk = importlib.import_module("lark_oapi.ws.client")
            sdk.loop = loop
            if self._stop_requested.is_set():
                return
            self._loop = loop
            self._client = _stoppable_client_class(sdk, self._stop_requested)(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=_UntilStopped(self._event_handler, self._stop_requested),
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
