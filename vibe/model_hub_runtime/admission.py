"""Engine-only admission leases, including continuations during a drain.

All state is owned by the controller asyncio loop. A drain holds neither the
routing nor installation lock, so already admitted OAuth/config work can finish.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from inspect import isawaitable
from typing import Any, Callable

from vibe.model_hub_runtime.supervisor import EngineUnavailableError

_CURRENT: ContextVar[tuple[EngineAdmission, EngineLease] | None] = ContextVar("engine_admission", default=None)


@dataclass(eq=False)
class EngineLease:
    active: bool = True
    references: int = 0
    retiring: bool = False
    interrupted: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)
    abort: Callable[[], Any] | None = None


class EngineAdmission:
    def __init__(self) -> None:
        self.reason: str | None = None
        self.leases: set[EngineLease] = set()
        self._closures: set[asyncio.Task] = set()
        self.idle = asyncio.Event()
        self.idle.set()

    def acquire(self, continuation: EngineLease | None = None) -> tuple[EngineLease, bool]:
        inherited = _CURRENT.get()
        lease = continuation or (inherited[1] if inherited and inherited[0] is self else None)
        if lease is not None and lease.active and lease in self.leases:
            if lease.interrupted:
                raise EngineUnavailableError("models.engine.withdrawn", reason="engine_withdrawn")
            return lease, False
        if self.reason is not None:
            raise EngineUnavailableError(
                "models.engine.withdrawn" if self.reason == "engine_withdrawn" else "models.engine.install_failed",
                reason=self.reason,
            )
        lease = EngineLease()
        self.leases.add(lease)
        self.idle.clear()
        return lease, True

    def retain(self) -> EngineLease:
        """An independently owned transport/flow continuation of admitted work."""
        inherited = _CURRENT.get()
        if inherited and inherited[0] is self and inherited[1].interrupted:
            raise EngineUnavailableError("models.engine.withdrawn", reason="engine_withdrawn")
        if self.reason and not (inherited and inherited[0] is self and inherited[1].active):
            raise EngineUnavailableError("models.engine.install_failed", reason=self.reason)
        lease = EngineLease()
        self.leases.add(lease)
        self.idle.clear()
        return lease

    def release(self, lease: EngineLease) -> None:
        if not lease.active:
            return
        if lease.references:
            lease.retiring = True
            return
        lease.active = False
        self.leases.discard(lease)
        if not self.leases:
            self.idle.set()

    def close(self, reason: str) -> None:
        self.reason = reason

    def reopen(self) -> None:
        self.reason = None

    async def interrupt(self) -> None:
        """One terminal signal per admitted unit, never an upstream replay."""
        current = asyncio.current_task()
        for lease in tuple(self.leases):
            if lease.interrupted:
                continue
            lease.interrupted = True
            for task in tuple(lease.tasks):
                if task is not current and not task.done():
                    task.cancel("engine_withdrawn")
            if lease.abort is not None:
                callback, lease.abort = lease.abort, None
                # Reserve BEFORE a synchronous callback can release the lease.
                lease.references += 1
                try:
                    result = callback()
                except Exception:
                    # A broken closer keeps this reference fenced; it is not
                    # permission to admit a replacement process or leave other
                    # admitted work unsignalled.
                    continue
                if isawaitable(result):
                    # Signal now, join cleanup separately. Its retained lease
                    # still forbids a replacement real-auth start while a close
                    # or cancelled mutable worker has not actually finished.
                    task = asyncio.create_task(self._close(result, lease))
                    self._closures.add(task)
                    task.add_done_callback(self._closures.discard)
                else:
                    lease.references -= 1
                    if lease.retiring:
                        self.release(lease)
        await asyncio.sleep(0)

    async def _close(self, result, lease: EngineLease) -> None:
        try:
            await result
        except Exception:
            pass  # terminal outcome is already published; admission stays fenced
        finally:
            lease.references -= 1
            if lease.retiring:
                self.release(lease)

    async def join_interruptions(self) -> None:
        tasks = tuple(self._closures)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def engine_operation(function):
    """Lease the complete operation, not merely the acquisition of a client."""
    @wraps(function)
    async def leased(self, *args, **kwargs):
        gate = self._engine_admission
        if self.lifecycle is not None:
            await self.recover_lifecycle()
            self._gate_known_withdrawal()
        flow = None
        if function.__name__ in {"oauth_status", "submit_oauth", "cancel_oauth"}:
            flow_id = args[0] if args else kwargs.get("flow_id")
            flow = self._oauth_flows.get(flow_id)
        lease, owned = gate.acquire(getattr(flow, "lease", None))
        task = asyncio.current_task()
        already_present = task in lease.tasks
        lease.tasks.add(task)
        lease.references += 1
        context = _CURRENT.set((gate, lease))
        try:
            result = await function(self, *args, **kwargs)
            # The lease is still held: a later swap cannot change the stamp of
            # a result whose service-side persistence happens after release.
            from core.handlers.model_hub.adapter import ModelInventory, OAuthFlowState, SourceObservation

            release = self.engine_release()
            if isinstance(result, (OAuthFlowState, SourceObservation)):
                result = replace(result, engine_release=result.engine_release or release)
            elif function.__name__ == "discover_models":
                result = ModelInventory(result, engine_release=release)
            elif function.__name__ == "subscription_quota" and isinstance(result, dict):
                result = {**result, "engine_release": release}
            return result
        except asyncio.CancelledError as exc:
            if gate.reason == "engine_withdrawn":
                raise EngineUnavailableError("models.engine.withdrawn", reason="engine_withdrawn") from exc
            raise
        finally:
            _CURRENT.reset(context)
            if not already_present:
                lease.tasks.discard(task)
            lease.references -= 1
            if owned or lease.retiring:
                gate.release(lease)
    return leased
