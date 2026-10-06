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
        self.stop_gate: asyncio.Event | None = None
        self.before_stop = None
        self.start_gate: asyncio.Event | None = None
        self.started = 0

    async def start(self, spec: _Spec) -> str:
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("start failed")
        runtime = f"{spec.digest}#{self.started}"
        self.started += 1
        self.live.append(runtime)
        return runtime

    async def stop(self, generation, force: bool) -> bool:
        if self.stop_gate is not None:
            await self.stop_gate.wait()
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
        await generations.settled()
        assert runtimes.stopped == [(first.generation.runtime, False)]
        assert runtimes.live == [replaced.generation.runtime]

        new = await asyncio.wait_for(generations.acquire(_Spec("c")), timeout=1)
        await generations.reap()
        assert generations.current is new.generation
        assert replaced.generation.retiring and replaced.generation.runtime in runtimes.live

        await replaced.release()
        await replaced.release()
        await generations.settled()
        assert runtimes.stopped[-1] == (replaced.generation.runtime, False)
        assert generations.generations == (new.generation,)

    asyncio.run(run())


def test_runtime_gen_003_at_the_cap_the_oldest_retiring_generation_gives_way():
    """RUNTIME-GEN-003: at the cap a new turn still starts at once.

    The oldest retiring generation is force-stopped, even while its adapter
    still reports work and its stop settles that work's bindings, and the unit
    is back at the cap.
    """
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=3)

    async def run():
        oldest = await generations.acquire(_Spec("1"))
        middle = await generations.acquire(_Spec("2"))
        current = await generations.acquire(_Spec("3"))

        async def settle_bound_work(generation):
            if generation is oldest.generation:
                await oldest.release()

        runtimes.before_stop = settle_bound_work

        newest = await asyncio.wait_for(generations.acquire(_Spec("4")), timeout=1)
        await generations.settled()

        assert runtimes.stopped == [(oldest.generation.runtime, True)]
        assert generations.generations == (middle.generation, current.generation, newest.generation)
        assert len(runtimes.live) == 3

    asyncio.run(run())


def test_the_cap_holds_when_graceful_stops_decline_or_are_still_running():
    """Generations whose adapters still report work, or whose stop is pending, count toward the cap."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=3)

    async def run():
        runtimes.decline_stops = 10
        for digest in ("1", "2", "3", "4", "5"):
            binding = await generations.acquire(_Spec(digest))
            await binding.release()
            await generations.settled()
            assert len(runtimes.live) <= 3

        assert runtimes.stopped == [("1#0", True), ("2#1", True)]
        assert [generation.spec.digest for generation in generations.generations] == ["3", "4", "5"]

        # A graceful stop still pending when more generations arrive is
        # rechecked against the cap once it declines.
        runtimes.stop_gate = asyncio.Event()
        for digest in ("6", "7"):
            await generations.acquire(_Spec(digest))
        runtimes.stop_gate.set()
        await generations.settled()
        assert len(runtimes.live) == 3

    asyncio.run(run())


def test_the_cap_still_reclaims_a_generation_whose_forced_stop_failed():
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=3)

    async def run():
        for digest in ("1", "2", "3"):
            await generations.acquire(_Spec(digest))
        runtimes.fail_stops = 1
        await generations.acquire(_Spec("4"))
        await generations.settled()
        # The oldest could not be stopped, so the next one gives way instead.
        assert runtimes.live == ["1#0", "3#2", "4#3"]
        assert runtimes.stopped == [("2#1", True)]

        # The failed generation stays eligible: the next pass reclaims it first.
        await generations.acquire(_Spec("5"))
        await generations.settled()
        assert runtimes.live == ["3#2", "4#3", "5#4"]
        assert runtimes.stopped == [("2#1", True), ("1#0", True)]

    asyncio.run(run())


def test_admission_never_waits_for_a_teardown():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        first = await generations.acquire(_Spec("a"))
        await first.release()
        runtimes.stop_gate = asyncio.Event()

        second = await asyncio.wait_for(generations.acquire(_Spec("b")), timeout=1)
        assert generations.current is second.generation
        assert first.generation.runtime in runtimes.live

        runtimes.stop_gate.set()
        await generations.settled()
        assert runtimes.stopped == [(first.generation.runtime, False)]

    asyncio.run(run())


def test_a_live_generation_with_the_needed_spec_serves_again():
    """Sessions alternating between two launch specs keep two processes, not more."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        hub = await generations.acquire(_Spec("hub"))
        native = await generations.acquire(_Spec("native"))
        again = await generations.acquire(_Spec("hub"))
        await generations.settled()
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
        await generations.settled()
        assert previous.retiring and "previous" in runtimes.live

        await restored.release()
        await generations.settled()
        assert runtimes.stopped == [("previous", False)]
        assert generations.generations == (current.generation,)

    asyncio.run(run())


def test_a_retired_generation_stops_once_unbound_and_admits_no_new_turn():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        bound = await generations.acquire(_Spec("a"))
        await generations.retire(bound.generation)
        await generations.settled()
        assert generations.current is None and bound.generation.runtime in runtimes.live

        fresh = await generations.acquire(_Spec("a"))
        assert fresh.generation is not bound.generation
        await bound.release()
        await generations.settled()
        assert runtimes.stopped == [(bound.generation.runtime, False)]

        await fresh.release()
        await generations.retire_current()
        await generations.settled()
        assert runtimes.stopped[-1] == (fresh.generation.runtime, False)
        await generations.retire(fresh.generation)
        await generations.retire_current()
        await generations.settled()
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

        await old.release()
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await generations.settled()
            runtimes.before_stop = None
        else:
            await generations.settled()
        assert old.generation in generations.generations and not old.generation.stopped

        again = await generations.acquire(_Spec("a"))
        assert (again.generation is old.generation) is (outcome == "declines")
        if again.generation is not old.generation:
            await generations.reap()
            assert runtimes.stopped[-1] == (old.generation.runtime, False)
            assert old.generation not in generations.generations

    asyncio.run(run())


@pytest.mark.parametrize("force", [True, False])
def test_stop_all_closes_admission_and_keeps_bound_work_on_a_graceful_stop(force):
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        serving = await generations.acquire(_Spec("a"))
        runtimes.start_gate = asyncio.Event()
        starting = asyncio.create_task(generations.acquire(_Spec("b")))
        await asyncio.sleep(0)

        stopping = asyncio.create_task(generations.stop_all(force=force))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeUnitStopping):
            await generations.acquire(_Spec("a"))
        # Stop-all covers the start that was already in flight.
        assert not stopping.done()
        runtimes.start_gate.set()
        await stopping
        with pytest.raises(RuntimeUnitStopping):
            await starting
        assert "b#1" not in runtimes.live

        if force:
            assert runtimes.live == []
        else:
            # The bound generation keeps its work until that work is released.
            assert runtimes.live == [serving.generation.runtime]
            await serving.release()
            await generations.settled()
            assert runtimes.live == []

    asyncio.run(run())


def test_a_start_that_lands_after_a_graceful_stop_all_costs_no_running_work():
    """A unit at the cap that stops gracefully never force-stops bound work for a late start."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=3)

    async def run():
        bound = [await generations.acquire(_Spec(digest)) for digest in ("1", "2", "3")]
        runtimes.start_gate = asyncio.Event()
        late = asyncio.create_task(generations.acquire(_Spec("4")))
        await asyncio.sleep(0)

        stopping = asyncio.create_task(generations.stop_all(force=False))
        await asyncio.sleep(0)
        runtimes.start_gate.set()
        await stopping
        with pytest.raises(RuntimeUnitStopping):
            await late

        # Only the late, unbound start stops; the bound work keeps running.
        assert runtimes.stopped == [("4#3", False)]
        for binding in bound:
            await binding.release()
        await generations.settled()
        assert runtimes.live == []
        assert all(not force for _runtime, force in runtimes.stopped)

    asyncio.run(run())


def test_a_forced_stop_all_forces_a_generation_whose_graceful_stop_was_in_flight():
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        old = await generations.acquire(_Spec("a"))
        await generations.acquire(_Spec("b"))
        runtimes.stop_gate = asyncio.Event()
        runtimes.decline_stops = 1
        await old.release()
        await asyncio.sleep(0)

        stopping = asyncio.create_task(generations.stop_all(force=True))
        await asyncio.sleep(0)
        runtimes.stop_gate.set()
        await asyncio.wait_for(stopping, timeout=1)

        assert runtimes.live == []
        assert (old.generation.runtime, True) in runtimes.stopped

    asyncio.run(run())


def test_a_forced_stop_all_retries_a_forced_stop_that_was_in_flight_and_failed():
    """A forced stop-all stops every process, even one whose earlier forced stop failed mid-pass."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set(cap=2)

    async def run():
        oldest = await generations.acquire(_Spec("1"))
        await generations.acquire(_Spec("2"))
        runtimes.stop_gate = asyncio.Event()
        runtimes.fail_stops = 1
        # The cap force-stops the oldest; that stop is still running.
        await generations.acquire(_Spec("3"))
        await asyncio.sleep(0)

        stopping = asyncio.create_task(generations.stop_all(force=True))
        await asyncio.sleep(0)
        runtimes.stop_gate.set()
        await asyncio.wait_for(stopping, timeout=1)

        assert runtimes.live == []
        assert (oldest.generation.runtime, True) in runtimes.stopped

    asyncio.run(run())


def test_a_reap_during_a_pass_retries_a_stop_that_pass_already_declined():
    """A sweep that arrives mid-pass still retries a generation that pass declined earlier."""
    runtimes = _Runtimes()
    generations = runtimes.generation_set()

    async def run():
        first = await generations.acquire(_Spec("a"))
        second = await generations.acquire(_Spec("b"))
        await generations.acquire(_Spec("c"))
        runtimes.decline_stops = 1
        gate = asyncio.Event()

        async def hold_the_pass(generation):
            if generation is second.generation:
                await gate.wait()

        runtimes.before_stop = hold_the_pass
        await first.release()
        await second.release()
        for _ in range(5):
            await asyncio.sleep(0)
        # The pass declined the first stop and is now stopping the second.
        assert first.generation.deferred and first.generation.runtime in runtimes.live

        sweep = asyncio.create_task(generations.reap())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(sweep, timeout=1)

        assert first.generation.runtime not in runtimes.live
        assert second.generation.runtime not in runtimes.live

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
