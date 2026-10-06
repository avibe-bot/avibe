"""Start the Web UI again when its process dies under a running service.

`vibe`, the restart job, the desktop host and the regression supervisor each
start the service and the Web UI as sibling processes, and the local ones then
return. Nothing was left to notice the UI dying on its own: a UI that crashed or
was killed stayed down, taking the local Web UI and the Avibe Cloud origin with
it, while the service kept serving IM, until someone ran `vibe` again. The
service is the long-lived owner of the runtime, so it keeps its Web surface up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable

from core.services.settings import load_config_or_default
from vibe import runtime
from vibe.upgrade import RestartState, restart_record_is_pending

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 10.0
MAX_RETRY_DELAY_SECONDS = 600.0
# Names the process that supervises the Web UI where one outlives the service,
# such as the Docker image's entrypoint. It stays the UI's only recovery owner.
UI_SUPERVISOR_ENV = "AVIBE_WEB_UI_SUPERVISOR"


def _restart_job_replacing_the_runtime() -> bool:
    """Whether a restart job is stopping and starting the runtime right now.

    A job still waiting out its delay has touched nothing yet, so a UI that dies
    meanwhile is not the job's to bring back.
    """

    path = runtime.get_restart_status_path()
    record = runtime.read_json(path)
    return (
        isinstance(record, dict)
        and record.get("state") == RestartState.RUNNING.value
        and restart_record_is_pending(record, path)
    )


class WebUiWatchdog:
    """Decides, one observation at a time, when the service starts its Web UI again."""

    def __init__(self, *, stopping: Callable[[], bool]) -> None:
        self._stopping = stopping
        self._gone_checks = 0
        self._attempts_without_recovery = 0
        self._retry_delay = CHECK_INTERVAL_SECONDS
        self._retry_at = 0.0
        self._unrecorded_ui_reported = False

    def _owns_recovery(self) -> bool:
        # Every deliberate stop takes the service down with the UI, and a
        # restart job brings the UI back itself.
        return not (
            self._stopping()
            or not runtime.current_process_owns_service_instance()
            or _restart_job_replacing_the_runtime()
        )

    def check(self, now: float) -> int | None:
        """Observe the UI once; return the pid of a UI this check started."""

        if not runtime.recorded_ui_is_gone():
            self._gone_checks = 0
            self._attempts_without_recovery = 0
            self._retry_delay = CHECK_INTERVAL_SECONDS
            self._retry_at = 0.0
            self._unrecorded_ui_reported = False
            return None
        # A stop, a restart, and a start replacing a stale UI each pass through
        # a moment with no live UI recorded. A UI gone at two consecutive checks
        # is gone; one gone at a single check may be in transit.
        self._gone_checks += 1
        if self._gone_checks < 2 or now < self._retry_at:
            return None
        self._gone_checks = 0

        config = load_config_or_default()
        host, port = runtime.effective_ui_bind_host(config), config.ui.setup_port
        if runtime.ui_server_compatible(host, port):
            # An Avibe UI, ready or not, holds the port without being the
            # recorded one. Another would only die on the port, and its pid
            # record would then name the dead replacement instead of it. No
            # start was attempted, so the retry delay stays where it was.
            if not self._unrecorded_ui_reported:
                logger.warning("Web UI on port %s is serving but is not the recorded UI process; leaving it", port)
                self._unrecorded_ui_reported = True
            return None
        # Asked last, after the probe that can take seconds, so a stop or a
        # restart that began meanwhile is seen before anything is started.
        if not self._owns_recovery():
            return None

        self._attempts_without_recovery += 1
        # A UI that keeps dying is retried less and less often instead of
        # being respawned every few seconds for as long as the service runs.
        self._retry_delay = min(self._retry_delay * 2, MAX_RETRY_DELAY_SECONDS)
        self._retry_at = now + self._retry_delay
        logger.warning(
            "Web UI is not running; starting it again (attempt %s since it was last seen running)",
            self._attempts_without_recovery,
        )
        pid = runtime.start_ui(host, port)
        if pid is None:
            logger.error("Web UI could not be started again")
            return None
        status = runtime.read_status()
        runtime.write_status(status.get("state", "running"), status.get("detail"), status.get("service_pid"), pid)
        logger.info("Web UI started again pid=%s", pid)
        return pid


async def watch_web_ui(stopping: Callable[[], bool], *, interval: float = CHECK_INTERVAL_SECONDS) -> None:
    """Keep the Web UI running until the service stops."""

    supervisor = os.environ.get(UI_SUPERVISOR_ENV)
    if supervisor:
        logger.info("Web UI recovery is left to its supervisor: %s", supervisor)
        return
    watchdog = WebUiWatchdog(stopping=stopping)
    while not stopping():
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(watchdog.check, time.monotonic())
        except Exception:
            logger.error("Web UI watchdog check failed", exc_info=True)
