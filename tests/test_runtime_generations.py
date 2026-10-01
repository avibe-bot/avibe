"""Runtime generation sets: new turns never wait for, nor interrupt, old work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from modules.agents.runtime_generations import RuntimeGenerationSet, RuntimeUnitStopping


@dataclass(frozen=True)
class _Spec:
    digest: str


class _Runtimes:
    """Fake runtimes; the test controls how their teardown behaves."""

    def __init__(self) -> None:
        self.live: list[str] = []
        self.stopped: list[tuple[str, bool]] = []
        self.fail_start = False
        self.fail_stops = 0
        self.decline_stops = 0
        self.before_stop = None
        self.start_gate: asyncio.Event | None = None

    async def start(self, spec: _Spec) -> str:
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("start failed")
        runtime = f"{spec.digest}#{len(self.live) + len(self.stopped)}"
        self.live.append(runtime)
        return runtime

    async def stop(self, generation, force: bool) -> bool:
        if self.before_stop is not None:
            await self.before_stop(generation)
        if self.fail_stops:
            self.fail_stops -= 1
            raise RuntimeError("teardown failed")
        if self.decline_stops and not force:
            self.decline_stops -= 1
            return False
        self.live.remove(generation.runtime)
        self.stopped.append((generation.runtime, force))
        return True

    def generation_set(self, **kwargs) -> RuntimeGenerationSet[_Spec, str]:
        return RuntimeGenerationSet(start=self.start, stop=self.stop, **kwargs)


def test_runtime_gen_002_a_busy_generation_finishes_while_new_turns_use_a_new_one():
    """RUNTIME-GEN-002: generation admission never waits for or interrupts old work.

    The same spec shares the current generation. A changed spec starts a new
    generation first; an unbound predecessor then stops, and a bound one keeps
    serving its work and stops when the last binding is released.
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

    The new generation starts first and the oldest retiring one is then
    force-stopped, so the unit is back at the cap. A forced stop that settles
    its work releases that work's bindings without deadlocking admission.
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
        assert len(runtimes.live) == 2 and runtimes.stopped == []

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


@pytest.mark.parametrize("outcome", ["fails", "declines", "cancelled"])
def test_a_generation_whose_teardown_does_not_finish_stays_tracked(outcome):
    """A failed, declined, or cancelled stop keeps the process in the set for the next sweep.

    A generation whose teardown failed is never promoted back to serve a turn.
    """
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        old = await generations.acquire(_Spec("a"))
        await generations.acquire(_Spec("b"))
        if outcome == "fails":
            runtimes.fail_stops = 1
        elif outcome == "declines":
            runtimes.decline_stops = 1
        else:
            async def cancel_teardown(_generation):
                raise asyncio.CancelledError

            runtimes.before_stop = cancel_teardown

        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await old.release()
            runtimes.before_stop = None
        else:
            await old.release()
        assert old.generation in generations.generations and not old.generation.stopped

        again = await generations.acquire(_Spec("a"))
        assert (again.generation is old.generation) is (outcome == "declines")
        if again.generation is not old.generation:
            await generations.reap()
            assert runtimes.stopped[-1] == (old.generation.runtime, False)
            assert old.generation not in generations.generations

    asyncio.run(run())


def test_stop_all_closes_admission_and_stops_a_start_in_flight():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        serving = await generations.acquire(_Spec("a"))
        runtimes.start_gate = asyncio.Event()
        starting = asyncio.create_task(generations.acquire(_Spec("b")))
        await asyncio.sleep(0)

        await generations.stop_all(force=True)
        runtimes.start_gate.set()
        with pytest.raises(RuntimeUnitStopping):
            await starting
        with pytest.raises(RuntimeUnitStopping):
            await generations.acquire(_Spec("a"))

        assert runtimes.live == []
        assert serving.generation.stopped

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
