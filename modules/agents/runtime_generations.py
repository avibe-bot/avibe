"""Runtime generations: one runtime unit's processes across launch changes.

A runtime unit (a Codex working directory, the OpenCode instance) serves new
turns on its current generation. When a turn needs a different launch spec, the
new generation starts and the old one retires: it keeps serving the work bound
to it and stops once that work is released. New turns never wait for old work;
at the cap, the oldest retiring generation is force-stopped instead.

Every bookkeeping section is synchronous, so the set never holds its lock
across an adapter call; ``start`` runs under a separate start lock.
Teardown happens after a generation is detached: the adapter's ``stop`` may
decline a graceful stop while its own evidence shows work, and a generation
whose stop declines, fails, or is cancelled is re-attached for the next sweep.

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


@dataclass(eq=False)
class RuntimeBinding(Generic[_S, _R]):
    """Work bound to one generation; it keeps that generation alive until released."""

    generation: RuntimeGeneration[_S, _R]
    _owner: "RuntimeGenerationSet[_S, _R]"
    released: bool = False

    async def release(self) -> None:
        """Release this binding; any task may call it, and only once counts."""
        await self._owner._release(self)


_Victims = list[tuple["RuntimeGeneration", bool]]


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

        A forced stop must end the process; returning False from one is treated
        like a failure and retried by the next sweep.
        """
        if cap < 2:
            raise ValueError("a runtime unit needs room for a current and a retiring generation")
        self._start = start
        self._stop = stop
        self._cap = cap
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._serials = itertools.count(1)
        self._current: RuntimeGeneration[_S, _R] | None = None
        self._retiring: list[RuntimeGeneration[_S, _R]] = []
        self._stopping = False

    @property
    def current(self) -> RuntimeGeneration[_S, _R] | None:
        return self._current

    @property
    def generations(self) -> tuple[RuntimeGeneration[_S, _R], ...]:
        """Every attached generation, oldest first."""
        return (*self._retiring, *((self._current,) if self._current is not None else ()))

    async def acquire(self, spec: _S) -> RuntimeBinding[_S, _R]:
        """Bind a new turn that needs ``spec`` to the generation that serves it."""
        victims: _Victims = []
        try:
            async with self._lock:
                binding = self._bind_serving_locked(spec, victims)
                if binding is not None:
                    return binding
            # Starts are serialized, but turns a live generation already serves
            # never queue behind one.
            async with self._start_lock:
                async with self._lock:
                    binding = self._bind_serving_locked(spec, victims)
                    if binding is not None:
                        return binding
                # A failed start leaves the current generation serving.
                runtime = await self._start(spec)
                async with self._lock:
                    generation = RuntimeGeneration(spec, runtime, next(self._serials))
                    if self._stopping:
                        victims.append((generation, True))
                        raise RuntimeUnitStopping("runtime unit is stopping")
                    self._install_current_locked(generation, victims)
                    return self._bind_locked(generation)
        finally:
            await self._teardown(victims)
            # The cap counts what is still attached once graceful stops have
            # answered: a declined stop keeps its generation alive.
            await self._enforce_cap()

    async def bind(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        """Bind recovered work, such as a restored poll, to its known generation."""
        async with self._lock:
            self._admitting_locked()
            if generation.stopped:
                raise RuntimeError("cannot bind work to a stopped runtime generation")
            return self._bind_locked(generation)

    async def adopt(self, spec: _S, runtime: _R, *, current: bool) -> RuntimeGeneration[_S, _R]:
        """Register a process found running, for example after a controller restart."""
        async with self._lock:
            self._admitting_locked()
            generation = RuntimeGeneration(spec, runtime, next(self._serials))
            if current:
                if self._current is not None:
                    self._add_retiring_locked(self._current)
                self._current = generation
            else:
                self._add_retiring_locked(generation)
            return generation

    async def retire(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Admit no new turn to a generation; it stops once its work is released.

        The decision is atomic with ``acquire``, so a turn binding concurrently
        either keeps the generation alive or binds elsewhere.
        """
        victims: _Victims = []
        async with self._lock:
            self._retire_locked(generation, victims)
        await self._teardown(victims)

    async def retire_current(self) -> None:
        """Retire whichever generation is current, for example when a backend is disabled."""
        victims: _Victims = []
        async with self._lock:
            if self._current is not None:
                self._retire_locked(self._current, victims)
        await self._teardown(victims)

    async def discard(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Forget a generation whose process already ended or was retired elsewhere."""
        async with self._lock:
            self._detach_locked(generation)

    async def reap(self) -> None:
        """Stop every retiring generation that no work is bound to."""
        victims: _Victims = []
        async with self._lock:
            self._collect_unbound_locked(victims)
        await self._teardown(victims)

    async def stop_all(self, *, force: bool) -> None:
        """Admit nothing more and stop every generation, for example at shutdown."""
        async with self._lock:
            self._stopping = True
            victims: _Victims = [(self._detach_locked(generation), force) for generation in self.generations]
        await self._teardown(victims)

    def _admitting_locked(self) -> None:
        if self._stopping:
            raise RuntimeUnitStopping("runtime unit is stopping")

    def _bind_serving_locked(self, spec: _S, victims: _Victims) -> RuntimeBinding[_S, _R] | None:
        self._admitting_locked()
        current = self._current
        if current is not None and current.spec.digest == spec.digest:
            return self._bind_locked(current)
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
        self._install_current_locked(match, victims)
        return self._bind_locked(match)

    def _install_current_locked(self, generation: RuntimeGeneration[_S, _R], victims: _Victims) -> None:
        previous = self._current
        self._current = generation
        if previous is not None and previous is not generation:
            self._add_retiring_locked(previous)
        # The new generation already serves, so an unbound predecessor stops
        # without a gap.
        self._collect_unbound_locked(victims)

    def _retire_locked(self, generation: RuntimeGeneration[_S, _R], victims: _Victims) -> None:
        if generation.stopped:
            return
        generation.closed = True
        if self._current is generation:
            self._current = None
        if generation not in self._retiring:
            self._add_retiring_locked(generation)
        if generation.bindings == 0:
            victims.append((self._detach_locked(generation), False))

    def _bind_locked(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        generation.bindings += 1
        return RuntimeBinding(generation, self)

    async def _release(self, binding: RuntimeBinding[_S, _R]) -> None:
        victims: _Victims = []
        async with self._lock:
            if binding.released:
                return
            binding.released = True
            generation = binding.generation
            generation.bindings -= 1
            if generation.retiring and not generation.stopped and generation.bindings == 0:
                victims.append((self._detach_locked(generation), False))
        await self._teardown(victims)

    def _collect_unbound_locked(self, victims: _Victims) -> None:
        for generation in list(self._retiring):
            if generation.bindings == 0:
                victims.append((self._detach_locked(generation), False))

    def _detach_locked(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeGeneration[_S, _R]:
        if generation in self._retiring:
            self._retiring.remove(generation)
        if self._current is generation:
            self._current = None
        generation.retiring = False
        generation.stopped = True
        return generation

    async def _teardown(self, victims: _Victims) -> None:
        # Runs outside the lock: a forced stop settles its work, and that work
        # releases its bindings through this set.
        for index, (generation, force) in enumerate(victims):
            try:
                stopped = await self._stop(generation, force)
            except asyncio.CancelledError:
                for pending, _force in victims[index:]:
                    self._reattach(pending, closed=True)
                raise
            except Exception:
                logger.warning(
                    "Runtime generation %s failed to stop; keeping it for the next sweep",
                    generation.serial,
                    exc_info=True,
                )
                self._reattach(generation, closed=True)
                continue
            if stopped is False:
                # The adapter still sees work beyond bindings. A forced stop that
                # declines is a failure; a graceful one simply waits for a sweep.
                self._reattach(generation, closed=force)

    def _reattach(self, generation: RuntimeGeneration[_S, _R], *, closed: bool) -> None:
        # Synchronous, so it is atomic with every other bookkeeping section and
        # safe inside a cancellation handler.
        generation.stopped = False
        generation.closed = generation.closed or closed
        if generation not in self._retiring:
            self._add_retiring_locked(generation)

    def _add_retiring_locked(self, generation: RuntimeGeneration[_S, _R]) -> None:
        # Oldest first, so the cap always gives way from the oldest work.
        generation.retiring = True
        self._retiring.append(generation)
        self._retiring.sort(key=lambda item: item.serial)

    async def _enforce_cap(self) -> None:
        async with self._lock:
            victims: _Victims = []
            while self._retiring and len(self._retiring) + (self._current is not None) > self._cap:
                # Never make the new turn wait: the oldest work gives way.
                victims.append((self._detach_locked(self._retiring[0]), True))
        await self._teardown(victims)
