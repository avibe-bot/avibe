"""Runtime generations: one runtime unit's processes across launch changes.

A runtime unit (a Codex working directory, the OpenCode instance) serves new
turns on its current generation. When a turn needs a different launch spec, the
new generation starts and the old one retires: it keeps serving the work bound
to it and stops once that work is released. New turns never wait for old work;
at the cap, the oldest retiring generation is force-stopped instead.

Admission is pure bookkeeping: every method that changes state does so in one
synchronous step and never awaits a teardown. One reconciler task per set owns
every stop. It runs them one at a time and re-evaluates the cap and the drained
generations after each, so a stop that declines, fails, or is cancelled simply
leaves its generation attached for the next pass.

Adapters own everything backend-specific: the spec (whose digest includes their
renewal epoch, so a manual restart is just a new spec), how a runtime starts and
stops, any evidence of work beyond bindings, and the notice a forced stop shows.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, Protocol, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_GENERATION_CAP = 3


class LaunchSpec(Protocol):
    """The process-level inputs of a unit; two specs are equal when their digests are."""

    @property
    def digest(self) -> str: ...


_S = TypeVar("_S", bound=LaunchSpec)
_R = TypeVar("_R")


class RuntimeUnitStopping(RuntimeError):
    """The unit is shutting down and admits no more work."""


@dataclass(eq=False)
class RuntimeGeneration(Generic[_S, _R]):
    """One running process of a unit, started from one launch spec."""

    spec: _S
    runtime: _R
    serial: int
    retiring: bool = False
    stopped: bool = False
    bindings: int = 0
    # Never promoted back to current: retired on purpose, or its teardown failed.
    closed: bool = False
    # Its graceful stop declined; the next sweep or release retries it.
    deferred: bool = False
    # Its stop failed; only the next sweep retries it, even at the cap.
    failed: bool = False


@dataclass(eq=False)
class RuntimeBinding(Generic[_S, _R]):
    """Work bound to one generation; it keeps that generation alive until released."""

    generation: RuntimeGeneration[_S, _R]
    _owner: "RuntimeGenerationSet[_S, _R]"
    released: bool = False

    async def release(self) -> None:
        """Release this binding; any task may call it, and only once counts."""
        self._owner._release(self)


class RuntimeGenerationSet(Generic[_S, _R]):
    """The generations of one runtime unit."""

    def __init__(
        self,
        *,
        start: Callable[[_S], Awaitable[_R]],
        stop: Callable[[RuntimeGeneration[_S, _R], bool], Awaitable[bool]],
        cap: int = DEFAULT_GENERATION_CAP,
    ) -> None:
        """``stop(generation, force)`` returns False to decline a graceful stop.

        A graceful stop runs only once no binding remains; the adapter declines
        while its own evidence still shows work. A forced stop must end the
        process. A stop that declines or fails is retried by the next sweep.
        """
        if cap < 2:
            raise ValueError("a runtime unit needs room for a current and a retiring generation")
        self._start = start
        self._stop = stop
        self._cap = cap
        self._start_lock = asyncio.Lock()
        self._serials = itertools.count(1)
        self._current: RuntimeGeneration[_S, _R] | None = None
        self._retiring: list[RuntimeGeneration[_S, _R]] = []
        self._stopping = False
        self._force_all = False
        self._reconciler: asyncio.Task[None] | None = None
        # The serials a reconciler pass has already tried, per kind of stop, so
        # a stop that keeps failing waits for a retry instead of looping.
        self._tried_forced: set[int] = set()
        self._tried_graceful: set[int] = set()
        # Bumped by every retry request; a stop that was running when one
        # arrived leaves its generation eligible for another attempt.
        self._retry_requests = 0

    @property
    def current(self) -> RuntimeGeneration[_S, _R] | None:
        return self._current

    @property
    def generations(self) -> tuple[RuntimeGeneration[_S, _R], ...]:
        """Every attached generation, oldest first."""
        return (*self._retiring, *((self._current,) if self._current is not None else ()))

    async def acquire(self, spec: _S) -> RuntimeBinding[_S, _R]:
        """Bind a new turn that needs ``spec`` to the generation that serves it."""
        binding = self._bind_serving(spec)
        if binding is not None:
            return binding
        # Starts are serialized, but turns a live generation already serves never
        # queue behind one.
        async with self._start_lock:
            binding = self._bind_serving(spec)
            if binding is not None:
                return binding
            # A failed start leaves the current generation serving.
            runtime = await self._start(spec)
            generation = RuntimeGeneration(spec, runtime, next(self._serials))
            if self._stopping:
                generation.closed = True
                self._add_retiring(generation)
                self._kick()
                raise RuntimeUnitStopping("runtime unit is stopping")
            self._install_current(generation)
            return self._bind(generation)

    async def bind(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        """Bind recovered work, such as a restored poll, to its known generation."""
        self._admitting()
        if generation.stopped:
            raise RuntimeError("cannot bind work to a stopped runtime generation")
        return self._bind(generation)

    async def adopt(self, spec: _S, runtime: _R, *, current: bool) -> RuntimeGeneration[_S, _R]:
        """Register a process found running, for example after a controller restart."""
        self._admitting()
        generation = RuntimeGeneration(spec, runtime, next(self._serials))
        if current:
            self._install_current(generation)
        else:
            self._add_retiring(generation)
            self._kick()
        return generation

    async def retire(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Admit no new turn to a generation; it stops once its work is released."""
        self._retire(generation)

    async def retire_current(self) -> None:
        """Retire whichever generation is current, for example when a backend is disabled."""
        if self._current is not None:
            self._retire(self._current)

    async def discard(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Forget a generation whose process already ended or was retired elsewhere."""
        self._detach(generation)

    async def reap(self) -> None:
        """Retry every retiring generation, then wait for the stops this starts."""
        self._request_retry()
        self._kick()
        await self.settled()

    async def stop_all(self, *, force: bool) -> None:
        """Admit nothing more and stop every generation, for example at shutdown.

        A graceful stop-all retires every generation: unbound ones stop now and
        bound ones stop when their work is released. A forced one stops all.
        """
        self._stopping = True
        self._force_all = self._force_all or force
        for generation in self.generations:
            generation.closed = True
            if generation is self._current:
                self._current = None
                self._add_retiring(generation)
        self._request_retry()
        self._kick()
        # A start already in flight attaches its runtime for teardown once it
        # returns; wait for it so every process started before admission closed
        # is covered.
        async with self._start_lock:
            pass
        await self.settled()

    async def settled(self) -> None:
        """Wait until the reconciler has no stop left to run."""
        while self._reconciler is not None and not self._reconciler.done():
            await asyncio.shield(self._reconciler)

    def _request_retry(self) -> None:
        """Entitle every generation to one more stop attempt, including one whose stop is running."""
        self._retry_requests += 1
        self._tried_forced.clear()
        self._tried_graceful.clear()
        for generation in self._retiring:
            generation.deferred = False
            generation.failed = False

    def _admitting(self) -> None:
        if self._stopping:
            raise RuntimeUnitStopping("runtime unit is stopping")

    def _bind_serving(self, spec: _S) -> RuntimeBinding[_S, _R] | None:
        self._admitting()
        current = self._current
        if current is not None and current.spec.digest == spec.digest:
            return self._bind(current)
        match = next(
            (item for item in reversed(self._retiring) if item.spec.digest == spec.digest and not item.closed),
            None,
        )
        if match is None:
            return None
        # A live generation already serves this spec, for example when sessions
        # alternate between two launch channels.
        self._retiring.remove(match)
        match.retiring = False
        match.deferred = False
        self._install_current(match)
        return self._bind(match)

    def _install_current(self, generation: RuntimeGeneration[_S, _R]) -> None:
        previous = self._current
        self._current = generation
        if previous is not None and previous is not generation:
            # The new generation already serves, so an unbound predecessor stops
            # without a gap.
            self._add_retiring(previous)
        self._kick()

    def _retire(self, generation: RuntimeGeneration[_S, _R]) -> None:
        if generation.stopped:
            return
        generation.closed = True
        if self._current is generation:
            self._current = None
        if generation not in self._retiring:
            self._add_retiring(generation)
        self._kick()

    def _bind(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        generation.bindings += 1
        return RuntimeBinding(generation, self)

    def _release(self, binding: RuntimeBinding[_S, _R]) -> None:
        if binding.released:
            return
        binding.released = True
        generation = binding.generation
        generation.bindings -= 1
        if generation.retiring and generation.bindings == 0:
            # Drained now, so its graceful stop deserves another attempt even
            # if this pass already tried one while it was still bound.
            generation.deferred = False
            self._tried_graceful.discard(generation.serial)
            self._kick()

    def _add_retiring(self, generation: RuntimeGeneration[_S, _R]) -> None:
        # Oldest first, so the cap always gives way from the oldest work.
        generation.retiring = True
        self._retiring.append(generation)
        self._retiring.sort(key=lambda item: item.serial)

    def _detach(self, generation: RuntimeGeneration[_S, _R]) -> None:
        if generation in self._retiring:
            self._retiring.remove(generation)
        if self._current is generation:
            self._current = None
        generation.retiring = False
        generation.stopped = True

    def _kick(self) -> None:
        if self._reconciler is None or self._reconciler.done():
            self._reconciler = asyncio.get_running_loop().create_task(self._reconcile())

    def _next_victim(self) -> tuple[RuntimeGeneration[_S, _R], bool] | None:
        # Each pass tries a generation at most once per kind of stop until a
        # retry is requested, so a stop that keeps failing never loops, while a
        # declined graceful stop can still be forced in this pass.
        unforced = [generation for generation in self._retiring if generation.serial not in self._tried_forced]
        if self._force_all and unforced:
            return unforced[0], True
        if not self._stopping and len(self._retiring) + (self._current is not None) > self._cap and unforced:
            # Never make a new turn wait: the oldest work gives way, whether its
            # adapter still reports work or an earlier stop failed. A stopping
            # unit admits no new turn, so a start that lands after admission
            # closed stops by itself instead of costing the oldest its work.
            return unforced[0], True
        drained = next(
            (
                generation
                for generation in unforced
                if generation.serial not in self._tried_graceful
                and generation.bindings == 0
                and not generation.deferred
                and not generation.failed
            ),
            None,
        )
        return (drained, False) if drained is not None else None

    async def _reconcile(self) -> None:
        self._tried_forced.clear()
        self._tried_graceful.clear()
        while (choice := self._next_victim()) is not None:
            generation, force = choice
            (self._tried_forced if force else self._tried_graceful).add(generation.serial)
            requests = self._retry_requests
            self._detach(generation)
            try:
                stopped = await self._stop(generation, force)
            except asyncio.CancelledError:
                self._reattach(generation, failed=True, retry=False)
                raise
            except Exception:
                logger.warning(
                    "Runtime generation %s failed to stop; keeping it for the next sweep",
                    generation.serial,
                    exc_info=True,
                )
                self._reattach(generation, failed=True, retry=self._retry_requests != requests)
                continue
            if stopped is False:
                # The adapter still sees work. A forced stop that declines is a
                # failure; a graceful one is retried by the next sweep or release.
                self._reattach(generation, failed=force, retry=self._retry_requests != requests)

    def _reattach(self, generation: RuntimeGeneration[_S, _R], *, failed: bool, retry: bool) -> None:
        """Track a generation whose stop did not finish.

        ``retry`` means a retry was requested while the stop ran, so the
        generation stays eligible for one more attempt in this pass.
        """
        generation.stopped = False
        if failed:
            # A process whose teardown went wrong never serves a turn again.
            generation.closed = True
        if not retry:
            if failed:
                generation.failed = True
            else:
                generation.deferred = True
        if generation not in self._retiring:
            self._add_retiring(generation)
