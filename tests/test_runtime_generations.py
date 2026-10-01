"""Runtime generation sets: new turns never wait for, nor interrupt, old work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from modules.agents.runtime_generations import RuntimeGenerationSet


@dataclass(frozen=True)
class _Spec:
    digest: str


class _Runtimes:
    """Fake runtimes; the test controls work the adapter sees beyond bindings."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.live: list[str] = []
        self.stopped: list[tuple[str, bool]] = []
        self.fail_start = False
        self.fail_stops = 0
        self.before_stop = None
        self.peak = 0

    async def start(self, spec: _Spec) -> str:
        if self.fail_start:
            raise RuntimeError("start failed")
        runtime = f"{spec.digest}#{len(self.live) + len(self.stopped)}"
        self.live.append(runtime)
        self.peak = max(self.peak, len(self.live))
        return runtime

    async def stop(self, generation, force: bool) -> None:
        if self.before_stop is not None:
            await self.before_stop(generation)
        if self.fail_stops:
            self.fail_stops -= 1
            raise RuntimeError("teardown failed")
        self.live.remove(generation.runtime)
        self.stopped.append((generation.runtime, force))

    async def idle(self, generation) -> bool:
        return generation.runtime not in self.busy

    def generation_set(self, **kwargs) -> RuntimeGenerationSet[_Spec, str]:
        return RuntimeGenerationSet(start=self.start, stop=self.stop, idle=self.idle, **kwargs)


def test_runtime_gen_002_a_busy_generation_finishes_while_new_turns_use_a_new_one():
    """RUNTIME-GEN-002: generation admission never waits for or interrupts old work.

    The same spec shares the current generation. A changed spec replaces an idle
    generation, starting the new process first. A busy generation keeps serving
    its bound work while a new one takes new turns, and stops gracefully once
    the last binding is released.
    """
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        first = await generations.acquire(_Spec("a"))
        same = await generations.acquire(_Spec("a"))
        assert same.generation is first.generation
        await first.release()
        await same.release()

        replaced = await generations.acquire(_Spec("b"))
        assert runtimes.stopped == [(first.generation.runtime, False)]
        assert runtimes.live == [replaced.generation.runtime]

        new = await asyncio.wait_for(generations.acquire(_Spec("c")), timeout=1)
        assert generations.current is new.generation
        assert replaced.generation.retiring
        assert replaced.generation.runtime in runtimes.live

        await generations.reap()
        assert replaced.generation.runtime in runtimes.live
        await replaced.release()
        await replaced.release()
        assert runtimes.stopped[-1] == (replaced.generation.runtime, False)
        assert generations.generations == (new.generation,)

    asyncio.run(run())


def test_runtime_gen_003_at_the_cap_the_oldest_retiring_generation_gives_way():
    """RUNTIME-GEN-003: at the cap a new turn still starts at once.

    The oldest retiring generation is force-stopped instead, once the new one
    serves, and the unit is back at the cap. A forced stop that settles its work
    releases that work's bindings without deadlocking admission.
    """
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=3)

    async def run():
        oldest = await generations.acquire(_Spec("1"))
        middle = await generations.acquire(_Spec("2"))
        current = await generations.acquire(_Spec("3"))
        assert runtimes.stopped == []

        async def settle_bound_work(generation):
            if generation is oldest.generation:
                await oldest.release()

        runtimes.before_stop = settle_bound_work

        newest = await asyncio.wait_for(generations.acquire(_Spec("4")), timeout=1)

        assert runtimes.stopped == [(oldest.generation.runtime, True)]
        assert generations.generations == (middle.generation, current.generation, newest.generation)
        assert len(runtimes.live) == 3

    asyncio.run(run())


def test_a_live_generation_with_the_needed_spec_serves_again():
    """Sessions alternating between two launch specs keep two processes, not more."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        hub = await generations.acquire(_Spec("hub"))
        native = await generations.acquire(_Spec("native"))
        again = await generations.acquire(_Spec("hub"))
        assert again.generation is hub.generation
        assert generations.current is hub.generation and native.generation.retiring
        assert runtimes.peak == 2 and runtimes.stopped == []

    asyncio.run(run())


def test_adopted_and_restored_work_keep_their_generation_alive():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        runtimes.live.append("previous")
        previous = await generations.adopt(_Spec("old"), "previous", current=False)
        restored = await generations.bind(previous)
        current = await generations.acquire(_Spec("new"))
        assert previous.retiring and "previous" in runtimes.live

        await restored.release()
        assert runtimes.stopped == [("previous", False)]
        assert generations.generations == (current.generation,)

    asyncio.run(run())


def test_a_retired_generation_stops_once_unbound_and_admits_no_new_turn():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        bound = await generations.acquire(_Spec("a"))
        await generations.retire(bound.generation)
        assert generations.current is None and bound.generation.runtime in runtimes.live

        fresh = await generations.acquire(_Spec("a"))
        assert fresh.generation is not bound.generation
        await bound.release()
        assert runtimes.stopped == [(bound.generation.runtime, False)]

        await fresh.release()
        await generations.retire_current()
        assert runtimes.stopped[-1] == (fresh.generation.runtime, False)
        await generations.retire(fresh.generation)
        await generations.retire_current()
        assert len(runtimes.stopped) == 2

    asyncio.run(run())


def test_a_generation_whose_stop_fails_stays_tracked_for_the_next_sweep():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        old = await generations.acquire(_Spec("a"))
        await generations.acquire(_Spec("b"))
        runtimes.fail_stops = 1
        await old.release()
        assert old.generation in generations.generations and not old.generation.stopped

        await generations.reap()
        assert runtimes.stopped == [(old.generation.runtime, False)]
        assert old.generation not in generations.generations

    asyncio.run(run())


def test_a_failed_start_leaves_the_current_generation_serving():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        first = await generations.acquire(_Spec("a"))
        runtimes.fail_start = True
        with pytest.raises(RuntimeError, match="start failed"):
            await generations.acquire(_Spec("b"))
        assert generations.current is first.generation
        assert not first.generation.retiring
        assert runtimes.live == [first.generation.runtime]

    asyncio.run(run())
