"""Runtime generations: one runtime unit's processes across launch changes.

A runtime unit (a Codex working directory, the OpenCode instance) serves new
turns on its current generation. When a turn needs a different launch spec, an
idle current generation is replaced. A busy one keeps serving the work bound to
it while a new generation takes new turns, and it stops once that work drains.
New turns never wait for old work; at the cap, the oldest retiring generation is
force-stopped instead.

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


@dataclass(eq=False)
class RuntimeGeneration(Generic[_S, _R]):
    """One running process of a unit, started from one launch spec."""

    spec: _S
    runtime: _R
    serial: int
    retiring: bool = False
    stopped: bool = False
    bindings: int = 0
    # Set by ``retire``: never promoted back to current.
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


class RuntimeGenerationSet(Generic[_S, _R]):
    """The generations of one runtime unit."""

    def __init__(
        self,
        *,
        start: Callable[[_S], Awaitable[_R]],
        stop: Callable[[RuntimeGeneration[_S, _R], bool], Awaitable[None]],
        idle: Callable[[RuntimeGeneration[_S, _R]], Awaitable[bool]],
        cap: int = DEFAULT_GENERATION_CAP,
    ) -> None:
        if cap < 2:
            raise ValueError("a runtime unit needs room for a current and a retiring generation")
        self._start = start
        self._stop = stop
        self._idle = idle
        self._cap = cap
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._serials = itertools.count(1)
        self._current: RuntimeGeneration[_S, _R] | None = None
        self._retiring: list[RuntimeGeneration[_S, _R]] = []

    @property
    def current(self) -> RuntimeGeneration[_S, _R] | None:
        return self._current

    @property
    def generations(self) -> tuple[RuntimeGeneration[_S, _R], ...]:
        """Every live generation, oldest first."""
        return (*self._retiring, *((self._current,) if self._current is not None else ()))

    async def acquire(self, spec: _S) -> RuntimeBinding[_S, _R]:
        """Bind a new turn that needs ``spec`` to the generation that serves it."""
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]] = []
        try:
            async with self._lock:
                binding = await self._bind_serving_locked(spec, victims)
                if binding is not None:
                    return binding
            # Starts are serialized, but turns a live generation already serves
            # never queue behind one.
            async with self._start_lock:
                async with self._lock:
                    binding = await self._bind_serving_locked(spec, victims)
                    if binding is not None:
                        return binding
                # A failed start leaves the current generation serving.
                runtime = await self._start(spec)
                async with self._lock:
                    generation = RuntimeGeneration(spec, runtime, next(self._serials))
                    previous = self._current
                    self._current = generation
                    if previous is not None:
                        previous.retiring = True
                        self._retiring.append(previous)
                    # The new generation already serves, so an idle predecessor
                    # stops without a gap.
                    await self._collect_idle_locked(victims)
                    while self._retiring and len(self._retiring) + 1 > self._cap:
                        # Never make the new turn wait: the oldest work gives way.
                        victims.append((self._detach_locked(self._retiring[0]), True))
                    return self._bind_locked(generation)
        finally:
            await self._teardown(victims)

    async def bind(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        """Bind recovered work, such as a restored poll, to its known generation."""
        async with self._lock:
            if generation.stopped:
                raise RuntimeError("cannot bind work to a stopped runtime generation")
            return self._bind_locked(generation)

    async def adopt(self, spec: _S, runtime: _R, *, current: bool) -> RuntimeGeneration[_S, _R]:
        """Register a process found running, for example after a controller restart."""
        async with self._lock:
            generation = RuntimeGeneration(spec, runtime, next(self._serials))
            if current:
                if self._current is not None:
                    self._current.retiring = True
                    self._retiring.append(self._current)
                self._current = generation
            else:
                generation.retiring = True
                self._retiring.append(generation)
            return generation

    async def retire(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Stop admitting to a generation; it stops as soon as it is unbound and idle.

        The decision is atomic with ``acquire``, so a turn binding concurrently
        either keeps the generation alive or binds elsewhere.
        """
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]] = []
        async with self._lock:
            await self._retire_locked(generation, victims)
        await self._teardown(victims)

    async def retire_current(self) -> None:
        """Retire whichever generation is current, for example when a backend is disabled."""
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]] = []
        async with self._lock:
            if self._current is not None:
                await self._retire_locked(self._current, victims)
        await self._teardown(victims)

    async def discard(self, generation: RuntimeGeneration[_S, _R]) -> None:
        """Forget a generation whose process already ended or was retired elsewhere."""
        async with self._lock:
            self._detach_locked(generation)

    async def reap(self) -> None:
        """Stop every retiring generation whose bound work has drained."""
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]] = []
        async with self._lock:
            await self._collect_idle_locked(victims)
        await self._teardown(victims)

    async def stop_all(self, *, force: bool) -> None:
        """Stop every generation, for example at shutdown."""
        async with self._lock:
            victims = [(self._detach_locked(generation), force) for generation in self.generations]
        await self._teardown(victims)

    async def _bind_serving_locked(
        self,
        spec: _S,
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]],
    ) -> RuntimeBinding[_S, _R] | None:
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
        self._current = match
        if current is not None:
            current.retiring = True
            self._retiring.append(current)
        binding = self._bind_locked(match)
        await self._collect_idle_locked(victims)
        return binding

    async def _retire_locked(
        self,
        generation: RuntimeGeneration[_S, _R],
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]],
    ) -> None:
        if generation.stopped:
            return
        generation.closed = True
        if self._current is generation:
            self._current = None
        if generation not in self._retiring:
            generation.retiring = True
            self._retiring.append(generation)
        if await self._is_idle(generation):
            victims.append((self._detach_locked(generation), False))

    def _bind_locked(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeBinding[_S, _R]:
        generation.bindings += 1
        return RuntimeBinding(generation, self)

    async def _release(self, binding: RuntimeBinding[_S, _R]) -> None:
        victims: list[tuple[RuntimeGeneration[_S, _R], bool]] = []
        async with self._lock:
            if binding.released:
                return
            binding.released = True
            generation = binding.generation
            generation.bindings -= 1
            if generation.retiring and not generation.stopped and await self._is_idle(generation):
                victims.append((self._detach_locked(generation), False))
        await self._teardown(victims)

    async def _is_idle(self, generation: RuntimeGeneration[_S, _R]) -> bool:
        return generation.bindings == 0 and await self._idle(generation)

    async def _collect_idle_locked(self, victims: list[tuple[RuntimeGeneration[_S, _R], bool]]) -> None:
        for generation in list(self._retiring):
            if await self._is_idle(generation):
                victims.append((self._detach_locked(generation), False))

    def _detach_locked(self, generation: RuntimeGeneration[_S, _R]) -> RuntimeGeneration[_S, _R]:
        if generation in self._retiring:
            self._retiring.remove(generation)
        if self._current is generation:
            self._current = None
        generation.retiring = False
        generation.stopped = True
        return generation

    async def _teardown(self, victims: list[tuple[RuntimeGeneration[_S, _R], bool]]) -> None:
        # Teardown runs outside the lock: a forced stop settles its work, and
        # that work releases its bindings through this set.
        for generation, force in victims:
            try:
                await self._stop(generation, force)
            except Exception:
                logger.warning(
                    "Runtime generation %s failed to stop; keeping it for the next sweep",
                    generation.serial,
                    exc_info=True,
                )
                async with self._lock:
                    generation.stopped = False
                    generation.retiring = True
                    self._retiring.append(generation)
