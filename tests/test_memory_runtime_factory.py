from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest

from config.v2_config import MemoryConfig, MemoryEndpointConfig, MemoryProcessingConfig
from core.memory.artifact import FakeMemoryArtifactManager
from core.memory.process import FakeEverOSProcessFactory
from tests.memory_runtime_factory import (
    MemoryRuntimeFactory,
    finalizing_memory_runtimes,
)

pytest_plugins = ("pytester",)


def _runtime(factory: MemoryRuntimeFactory, home: Path):
    return factory(
        MemoryConfig(enabled=False),
        artifact_manager=FakeMemoryArtifactManager(),
        effective_home=home,
    )


async def test_factory_closes_runtimes_in_reverse_creation_order(tmp_path: Path) -> None:
    closed: list[str] = []

    async with finalizing_memory_runtimes() as factory:
        first = _runtime(factory, tmp_path / "first")
        second = _runtime(factory, tmp_path / "second")

        async def close_first() -> None:
            closed.append("first")

        async def close_second() -> None:
            closed.append("second")

        first.close = close_first
        second.close = close_second

    assert closed == ["second", "first"]


@pytest.mark.parametrize("failure", [AssertionError("assertion"), RuntimeError("test")])
async def test_factory_finalizes_when_the_owned_scope_raises(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    closed = asyncio.Event()

    with pytest.raises(type(failure), match=str(failure)):
        async with finalizing_memory_runtimes() as factory:
            runtime = _runtime(factory, tmp_path)

            async def close() -> None:
                closed.set()

            runtime.close = close
            raise failure

    assert closed.is_set()


async def test_factory_finalizes_when_async_setup_raises(tmp_path: Path) -> None:
    closed = asyncio.Event()

    @asynccontextmanager
    async def failing_setup() -> AsyncIterator[MemoryRuntimeFactory]:
        async with finalizing_memory_runtimes() as factory:
            runtime = _runtime(factory, tmp_path)

            async def close() -> None:
                closed.set()

            runtime.close = close
            raise RuntimeError("setup failed before yield")
            yield factory

    with pytest.raises(RuntimeError, match="setup failed before yield"):
        async with failing_setup():
            raise AssertionError("setup must not reach the test body")

    assert closed.is_set()


async def test_factory_finalizes_when_the_owned_scope_is_cancelled(tmp_path: Path) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def run() -> None:
        async with finalizing_memory_runtimes() as factory:
            runtime = _runtime(factory, tmp_path)

            async def close() -> None:
                closed.set()

            runtime.close = close
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run(), name="cancelled-runtime-owner")
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert closed.is_set()
    assert task.done()


async def test_factory_early_close_is_not_repeated_at_teardown(tmp_path: Path) -> None:
    close_calls = 0

    async with finalizing_memory_runtimes() as factory:
        runtime = _runtime(factory, tmp_path)

        async def close() -> None:
            nonlocal close_calls
            close_calls += 1

        runtime.close = close
        await factory.close(runtime)

    assert close_calls == 1


async def test_factory_teardown_joins_a_blocked_explicit_close(tmp_path: Path) -> None:
    factory = MemoryRuntimeFactory()
    runtime = _runtime(factory, tmp_path)
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_entered.set()
        await release_close.wait()

    runtime.close = close
    explicit_close = asyncio.create_task(factory.close(runtime))
    teardown: asyncio.Task[None] | None = None
    results: list[object] = []
    try:
        await asyncio.wait_for(close_entered.wait(), timeout=1.0)
        teardown = asyncio.create_task(factory.close_all())
        await asyncio.sleep(0)
        assert close_calls == 1
    finally:
        release_close.set()
        results.extend(
            await asyncio.gather(
                explicit_close,
                *(() if teardown is None else (teardown,)),
                return_exceptions=True,
            )
        )

    assert results == [None, None]
    await factory.close(runtime)
    assert close_calls == 1


async def test_factory_teardown_joins_close_after_explicit_caller_cancellation(
    tmp_path: Path,
) -> None:
    factory = MemoryRuntimeFactory()
    runtime = _runtime(factory, tmp_path)
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_entered.set()
        await release_close.wait()

    runtime.close = close
    explicit_close = asyncio.create_task(factory.close(runtime))
    teardown: asyncio.Task[None] | None = None
    results: list[object] = []
    try:
        await asyncio.wait_for(close_entered.wait(), timeout=1.0)
        explicit_close.cancel()
        with pytest.raises(asyncio.CancelledError):
            await explicit_close

        teardown = asyncio.create_task(factory.close_all())
        await asyncio.sleep(0)
        assert close_calls == 1
    finally:
        release_close.set()
        results.extend(
            await asyncio.gather(
                explicit_close,
                *(() if teardown is None else (teardown,)),
                return_exceptions=True,
            )
        )

    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1:] == [None]
    await factory.close(runtime)
    assert close_calls == 1


async def test_factory_shares_an_in_flight_close_failure_with_teardown_and_repeats(
    tmp_path: Path,
) -> None:
    factory = MemoryRuntimeFactory()
    runtime = _runtime(factory, tmp_path)
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    close_calls = 0

    async def close() -> None:
        nonlocal close_calls
        close_calls += 1
        close_entered.set()
        await release_close.wait()
        raise RuntimeError("shared close failure")

    runtime.close = close
    explicit_close = asyncio.create_task(factory.close(runtime))
    teardown: asyncio.Task[None] | None = None
    results: list[object] = []
    try:
        await asyncio.wait_for(close_entered.wait(), timeout=1.0)
        teardown = asyncio.create_task(factory.close_all())
        await asyncio.sleep(0)
        assert close_calls == 1
    finally:
        release_close.set()
        results.extend(
            await asyncio.gather(
                explicit_close,
                *(() if teardown is None else (teardown,)),
                return_exceptions=True,
            )
        )

    assert len(results) == 2
    assert all(
        isinstance(result, RuntimeError) and str(result) == "shared close failure"
        for result in results
    )
    assert results[0] is results[1]
    with pytest.raises(RuntimeError, match="shared close failure"):
        await factory.close(runtime)
    assert close_calls == 1


async def test_factory_settles_runtime_tasks_and_sidecar_before_scope_exit(
    tmp_path: Path,
) -> None:
    process_factory = FakeEverOSProcessFactory()
    processing = MemoryProcessingConfig(
        llm=MemoryEndpointConfig("https://llm.example.test/v1", "chat", "llm-key"),
        embedding=MemoryEndpointConfig(
            "https://embed.example.test/v1",
            "embed",
            "embed-key",
        ),
    )

    async with finalizing_memory_runtimes() as factory:
        runtime = factory(
            MemoryConfig(enabled=True, processing=processing),
            artifact_manager=FakeMemoryArtifactManager(
                python=Path(__file__),
                root_format="everos-test",
                fingerprint="test-artifact",
            ),
            process_factory=process_factory,
            effective_home=tmp_path,
        )
        assert await runtime.reconcile(runtime._config) == {
            "ok": True,
            "state": "running",
        }
        runtime.module._writer._ensure_worker()
        writer_task = runtime.module._writer._worker_task
        scheduler_task = runtime.module._writer._scheduler_task
        process = process_factory.supervised[0]
        assert writer_task is not None
        assert scheduler_task is not None

    assert process.stopped is True
    assert writer_task.done()
    assert writer_task not in asyncio.all_tasks()
    assert scheduler_task.done()
    assert scheduler_task not in asyncio.all_tasks()
    assert runtime.module._writer._worker_task is None
    assert runtime.module._writer._scheduler_task is None
    assert runtime._process is None


async def test_factory_continues_teardown_after_a_close_failure(tmp_path: Path) -> None:
    first_closed = asyncio.Event()

    with pytest.raises(RuntimeError, match="deliberate close failure"):
        async with finalizing_memory_runtimes() as factory:
            first = _runtime(factory, tmp_path / "first")
            second = _runtime(factory, tmp_path / "second")

            async def close_first() -> None:
                first_closed.set()

            async def close_second() -> None:
                raise RuntimeError("deliberate close failure")

            first.close = close_first
            second.close = close_second

    assert first_closed.is_set()


def test_fixture_reports_close_failure_without_hiding_test_failure(
    pytester: pytest.Pytester,
) -> None:
    project_root = Path(__file__).parents[1]
    pytester.makeconftest(
        f"""
import sys
sys.path.insert(0, {str(project_root)!r})

import pytest_asyncio

from tests.memory_runtime_factory import finalizing_memory_runtimes


@pytest_asyncio.fixture
async def memory_runtime_factory():
    async with finalizing_memory_runtimes() as factory:
        yield factory
"""
    )
    pytester.makepyfile(
        f"""
import sys
sys.path.insert(0, {str(project_root)!r})

import pytest

from config.v2_config import MemoryConfig
from core.memory.artifact import FakeMemoryArtifactManager


@pytest.mark.asyncio
async def test_original_failure_remains_visible(memory_runtime_factory, tmp_path):
    runtime = memory_runtime_factory(
        MemoryConfig(enabled=False),
        artifact_manager=FakeMemoryArtifactManager(),
        effective_home=tmp_path,
    )

    async def close():
        raise RuntimeError("deliberate teardown close failure")

    runtime.close = close
    raise AssertionError("original test failure")
"""
    )

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(failed=1, errors=1)
    output = result.stdout.str()
    assert "original test failure" in output
    assert "deliberate teardown close failure" in output
