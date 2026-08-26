"""Shared backend restart barrier and bounded drain coordinator."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_TIMEOUT_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 0.1


def _configured_drain_timeout() -> float:
    raw = os.environ.get("AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else DEFAULT_DRAIN_TIMEOUT_SECONDS
    except ValueError:
        logger.warning("Ignoring invalid AVIBE_BACKEND_RESTART_DRAIN_TIMEOUT_SECONDS=%r", raw)
        return DEFAULT_DRAIN_TIMEOUT_SECONDS


class BackendRestartCoordinator:
    """Serialize backend cutovers without stopping Avibe's service process."""

    def __init__(
        self,
        controller: Any,
        refresh: Callable[..., Awaitable[None]],
        *,
        preflight: Callable[[str], Awaitable[object]] | None = None,
        drain_timeout: float | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self.controller = controller
        self._refresh = refresh
        self._preflight = preflight
        self._drain_timeout = _configured_drain_timeout() if drain_timeout is None else max(0.0, drain_timeout)
        self._poll_interval = max(0.001, poll_interval)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_refreshes: dict[str, object] = {}
        self._request_locks: dict[str, asyncio.Lock] = {}

    async def request_restart(self, backend: str) -> str:
        """Begin or join a restart, waiting only when no work is draining."""
        lock = self._request_locks.setdefault(backend, asyncio.Lock())
        async with lock:
            existing = self._tasks.get(backend)
            if existing is not None and not existing.done():
                if self._preflight is not None:
                    prepared = await self._preflight(backend)
                    self._pending_refreshes[backend] = prepared
                task = existing
                had_active_work = await self._has_active_turns(backend)
            else:
                agent_service = self.controller.agent_service
                session_turns = self.controller.session_turns
                agent_service.begin_backend_drain(backend)
                session_turns.begin_backend_drain(backend)
                try:
                    await agent_service.prepare_backend_restart(backend)
                    prepared = await self._preflight(backend) if self._preflight is not None else None
                except Exception:
                    agent_service.end_backend_drain(backend)
                    await session_turns.end_backend_drain(backend, resume_deferred=False)
                    raise
                had_active_work = await self._has_active_turns(backend)
                task = asyncio.create_task(
                    self._run(backend, prepared),
                    name=f"backend-restart:{backend}",
                )
                self._tasks[backend] = task
                task.add_done_callback(lambda completed, name=backend: self._on_done(name, completed))

        # An idle acknowledgement means the requested generation is live. Only
        # an active drain may return before the cutover itself completes.
        if not had_active_work:
            await task
            return "restarted"
        return "draining"

    def _on_done(self, backend: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(backend) is task:
            self._tasks.pop(backend, None)
        try:
            task.result()
        except asyncio.CancelledError:
            logger.info("Backend restart cancelled for %s", backend)
        except Exception:
            logger.exception("Backend restart failed for %s", backend)

    async def _has_active_turns(self, backend: str) -> bool:
        service = self.controller.agent_service
        if service.runtime_turn_tokens_for_backend(backend):
            return True
        probe = getattr(service, "backend_runtime_active", None)
        if not callable(probe):
            return False
        result = probe(backend)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    async def _run(self, backend: str, prepared: object) -> None:
        forced = False
        finalized = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drain_timeout
        try:
            while await self._has_active_turns(backend):
                if loop.time() >= deadline:
                    forced = True
                    session_ids = self.controller.session_turns.active_runtime_session_ids_for_backend(backend)
                    await self.controller.session_turns.release_for_backend_refresh(
                        backend=backend,
                        base_session_ids=session_ids,
                    )
                    await self.controller.agent_service.force_cancel_backend_turns(backend)
                    self.controller.agent_service.force_end_backend_activities(backend)
                    break
                await asyncio.sleep(self._poll_interval)
            while True:
                try:
                    if self._preflight is None:
                        await self._refresh(backend, forced)
                    else:
                        await self._refresh(backend, forced, prepared)
                except Exception:
                    lock = self._request_locks.setdefault(backend, asyncio.Lock())
                    async with lock:
                        next_prepared = self._pending_refreshes.pop(backend, None)
                        if next_prepared is None:
                            raise
                    logger.exception(
                        "Backend refresh failed for %s; applying the latest joined request",
                        backend,
                    )
                    prepared = next_prepared
                    continue

                lock = self._request_locks.setdefault(backend, asyncio.Lock())
                async with lock:
                    next_prepared = self._pending_refreshes.pop(backend, None)
                    if next_prepared is not None:
                        prepared = next_prepared
                        continue
                    # Runtime admission opens before durable queues are flushed. A
                    # flush therefore always enters the latest refreshed generation.
                    self.controller.agent_service.end_backend_drain(backend)
                    await self.controller.session_turns.end_backend_drain(
                        backend,
                        resume_deferred=True,
                    )
                    current = asyncio.current_task()
                    if self._tasks.get(backend) is current:
                        self._tasks.pop(backend, None)
                    finalized = True
                    return
        finally:
            if not finalized:
                lock = self._request_locks.setdefault(backend, asyncio.Lock())
                async with lock:
                    self._pending_refreshes.pop(backend, None)
                    self.controller.agent_service.end_backend_drain(backend)
                    await self.controller.session_turns.end_backend_drain(
                        backend,
                        resume_deferred=False,
                    )
                    current = asyncio.current_task()
                    if self._tasks.get(backend) is current:
                        self._tasks.pop(backend, None)

    async def wait(self, backend: str) -> None:
        """Testing/diagnostic hook: wait for the current restart, if any."""
        task = self._tasks.get(backend)
        if task is not None:
            await asyncio.shield(task)
