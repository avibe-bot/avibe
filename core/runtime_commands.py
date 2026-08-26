"""Polls a shared directory for inter-process runtime command markers.

The UI server runs in its own process (see ``vibe/cli.py::cmd_vibe``) and
cannot reach into the controller's in-memory agent state. When the user
clicks "Restart" on a backend, the UI server drops a marker file here so
the controller can run the matching in-process cleanup
(``AgentAuthService._refresh_backend_runtime``) instead of killing the
subprocess out-of-band and leaving stale transports cached in the
controller.

The marker contract is intentionally minimal: file name encodes the
command and target, contents are advisory only, file presence == request,
file deletion == acknowledgement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from config import paths
from modules.agents.catalog import supports_runtime_refresh

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.controller import Controller

logger = logging.getLogger(__name__)

# The optional ``.<reqid>`` suffix lets each restart get its own marker
# (and ``.err`` companion). The UI server's ``_request_controller_restart``
# always sends a reqid; we still accept the bare ``restart-<backend>.cmd``
# form so any leftover marker from an older client doesn't get ignored.
_MARKER_PATTERN = re.compile(
    r"^restart-(?P<backend>[a-z][a-z0-9_-]*)(?:\.(?P<reqid>[A-Za-z0-9_-]+))?\.cmd$"
)
_ERR_SUFFIX = ".cmd.err"
# .err companions older than this are assumed orphaned (the requester never
# polled — e.g. the UI server was restarted mid-flight) and may be cleaned
# up. The window is generous because ``_wait_for_controller_ack`` waits up
# to ~4s; anything older than five minutes is safely past every caller.
_ERR_STALE_AGE_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.5


class RuntimeCommandWatcher:
    """Background task that drains command markers and dispatches them."""

    def __init__(
        self,
        controller: "Controller",
        *,
        directory: Optional[Path] = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self.controller = controller
        self._directory = directory or (paths.get_state_dir() / "runtime_commands")
        self._poll_interval = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Runtime command watcher disabled: %s", exc)
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="runtime-command-watcher")
        logger.info("Runtime command watcher started at %s", self._directory)

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if not task:
            return
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._sweep_once()
            except Exception as exc:  # pragma: no cover - keep loop alive
                logger.warning("Runtime command sweep failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _sweep_once(self) -> None:
        try:
            entries = list(self._directory.iterdir())
        except FileNotFoundError:
            return
        for marker in entries:
            if not marker.is_file():
                continue
            # ``.cmd.err`` companions are status output for the requester;
            # leaving them in place is required so ``_wait_for_controller_ack``
            # can read them before the next sweep. Only drop ones that are
            # demonstrably orphaned so the directory does not grow without
            # bound when a requester crashes mid-flight.
            if marker.name.endswith(_ERR_SUFFIX):
                self._maybe_evict_stale_err(marker)
                continue
            match = _MARKER_PATTERN.match(marker.name)
            if not match:
                logger.debug("Ignoring unknown runtime command marker: %s", marker.name)
                marker.unlink(missing_ok=True)
                continue
            backend = match.group("backend")
            if not supports_runtime_refresh(backend):
                logger.debug("Ignoring unsupported backend marker: %s", backend)
                marker.unlink(missing_ok=True)
                continue
            await self._handle_restart(backend, marker)

    @staticmethod
    def _maybe_evict_stale_err(err_marker: Path) -> None:
        """Delete a ``.err`` companion only if it is clearly orphaned.

        The requester reads ``.err`` files at most a few seconds after the
        controller writes them; anything older is past every caller's
        polling window and can be reclaimed. We deliberately do *not*
        delete fresh ones — that would race against
        ``_wait_for_controller_ack`` and silently demote a real failure to
        a "restart succeeded" toast.
        """
        try:
            age = time.time() - err_marker.stat().st_mtime
        except OSError:
            return
        if age < _ERR_STALE_AGE_SECONDS:
            return
        try:
            err_marker.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - best-effort cleanup
            logger.debug("Could not evict stale err marker %s: %s", err_marker, exc)

    async def _handle_restart(self, backend: str, marker: Path) -> None:
        metadata = self._read_marker_metadata(marker)
        if metadata:
            logger.info("Runtime command: refresh backend=%s metadata=%s", backend, metadata)
        else:
            logger.info("Runtime command: refresh backend=%s", backend)
        handler: Optional[Callable[[str], Awaitable[None]]] = getattr(
            self.controller.agent_auth_service, "_refresh_backend_runtime", None
        )
        if handler is None:
            logger.warning("AgentAuthService missing _refresh_backend_runtime; dropping marker")
            self._fail_marker(marker, "refresh handler unavailable")
            return
        try:
            await handler(backend)
        except Exception as exc:
            logger.error("Backend refresh failed for %s: %s", backend, exc, exc_info=True)
            self._fail_marker(marker, str(exc) or exc.__class__.__name__)
            return
        # Success path: silently drop the marker so the UI server reads the
        # absence-of-``.err`` as a clean ack.
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - best-effort cleanup
            logger.debug("Could not remove marker %s: %s", marker, exc)

    @staticmethod
    def _read_marker_metadata(marker: Path) -> dict:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _fail_marker(marker: Path, error: str) -> None:
        """Surface a refresh failure to the UI server via a companion ``.err``.

        The caller (``vibe.api._wait_for_controller_ack``) treats marker
        deletion as the ack and looks for ``marker.name + ".err"`` to decide
        whether the refresh actually succeeded — otherwise the UI would
        toast ``ok: true`` while the runtime is still stale.
        """
        err_marker = marker.with_name(marker.name + ".err")
        try:
            err_marker.write_text(error[:1024], encoding="utf-8")
        except OSError as exc:  # pragma: no cover - best-effort surface
            logger.debug("Could not write error marker %s: %s", err_marker, exc)
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - best-effort cleanup
            logger.debug("Could not remove marker %s: %s", marker, exc)
