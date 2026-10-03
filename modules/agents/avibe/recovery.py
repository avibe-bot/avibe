"""The single owner of the Avibe Agent's settlement outside a Turn.

Settlement outside a Turn is T2 (open tool calls), T3 admission (accepted inputs
no run consumed), and J5 (pruning settled jobs). The controller holds one
``AvibeRecovery`` and so at most one recovery task: two adapters settling the
same Session would each take their own locks and could both append a
``tool_result``.

A pass runs on the registered adapter, or, while ``agents.avibe`` is disabled,
on one unregistered adapter it keeps for as long as it needs one: recovery
needs only the transcript store, the job host, and the Watch hand-over. Every
pass re-derives its candidates from the rows, so the unsettled set carries over
to whichever adapter runs the next pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A pass that left Sessions unsettled is retried after 5 s, 30 s, 2 min, then every 10 min.
RETRY_DELAYS_S = (5.0, 30.0, 120.0)
RETRY_PERIOD_S = 600.0


class AvibeRecovery:
    def __init__(self, controller: Any) -> None:
        self._controller = controller
        self._task: Optional[asyncio.Task] = None
        self._transient: Any = None
        # Serializes every pass, immediate or retried, and the scheduling around it, so
        # two passes never run at once (a start racing a registration, or a retry).
        self._lock = asyncio.Lock()

    @property
    def retrying(self) -> bool:
        """Whether some Session is still unsettled and a retry is pending."""
        return self._task is not None and not self._task.done()

    async def start(self) -> list[str]:
        """Retire any pending retry, run one pass now, and keep retrying while Sessions stay unsettled.

        The entry point for startup and for live registration of the backend. Under
        the lock a pending retry is only ever sleeping or waiting for the lock, never
        mid-pass, so it is retired without interrupting a write and there are never
        two passes at once. Returns the Sessions this pass left unsettled.
        """
        async with self._lock:
            await self.stop()
            unsettled = await self._pass()
            if unsettled and not self.retrying:
                self._task = asyncio.create_task(self._retry(), name="avibe-agent-recovery")
        return unsettled

    def ensure(self) -> None:
        """A Turn could not settle something (a returned input it failed to admit): retry it."""
        if not self.retrying:
            self._task = asyncio.create_task(self._retry(), name="avibe-agent-recovery")

    async def stop(self) -> None:
        """Stop retrying (service stop, or a new owner taking over); the next start recovers again."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _retry(self) -> None:
        # No kill fail-safe: a transient DB or Watch failure must not kill a user's command,
        # whose own wrapper deadline still bounds it.
        for attempt in itertools.count():
            await asyncio.sleep(RETRY_DELAYS_S[attempt] if attempt < len(RETRY_DELAYS_S) else RETRY_PERIOD_S)
            async with self._lock:
                unsettled = await self._pass()
            if not unsettled:
                logger.info("Avibe Agent recovery settled every Session")
                return
            logger.error("Avibe Agent recovery could not settle Sessions %s; retrying", ", ".join(unsettled))

    async def _pass(self) -> list[str]:
        agent = self._agent()
        try:
            unsettled = await agent.recover_runtime_state()
        except Exception:
            logger.exception("Avibe Agent recovery pass failed")
            return ["*"]
        if not unsettled:
            self._transient = None
        return unsettled

    def _agent(self) -> Any:
        registered = getattr(getattr(self._controller, "agent_service", None), "agents", {}).get("avibe")
        if registered is not None:
            self._transient = None
            return registered
        if self._transient is None:
            from modules.agents.avibe.agent import AvibeAgent

            self._transient = AvibeAgent(self._controller)
        return self._transient
