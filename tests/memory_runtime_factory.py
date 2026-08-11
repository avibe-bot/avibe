"""Finalizing ownership for test-created Memory runtimes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

from config.v2_config import MemoryConfig
from core.memory.artifact import MemoryArtifactPort
from core.memory.everos_insight.reader import MemoryInsightReader
from core.memory.process import EverOSProcessFactory
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.worker import ProcessingEvent


@dataclass
class _OwnedRuntime:
    runtime: MemoryRuntime
    close_task: asyncio.Task[None] | None = None


class MemoryRuntimeFactory:
    """Construct and finalize explicitly configured runtimes for one test."""

    def __init__(self) -> None:
        self._runtimes: list[_OwnedRuntime] = []
        self._closed_runtimes: list[MemoryRuntime] = []

    def __call__(
        self,
        config: MemoryConfig,
        *,
        store: MemoryStore | None = None,
        artifact_manager: MemoryArtifactPort | None = None,
        process_factory: EverOSProcessFactory | None = None,
        effective_home: Path | None = None,
        processing_event: ProcessingEvent | None = None,
        insight_reader: MemoryInsightReader | None = None,
        on_config_settled: Callable[[MemoryConfig], None] | None = None,
    ) -> MemoryRuntime:
        runtime = MemoryRuntime(
            config,
            store=store,
            artifact_manager=artifact_manager,
            process_factory=process_factory,
            effective_home=effective_home,
            processing_event=processing_event,
            insight_reader=insight_reader,
            on_config_settled=on_config_settled,
        )
        self._runtimes.append(_OwnedRuntime(runtime))
        return runtime

    def register(self, runtime: MemoryRuntime) -> MemoryRuntime:
        """Own a runtime constructed through the production factory under test."""

        if self._find_entry(runtime) is not None or self._is_closed(runtime):
            raise ValueError("MemoryRuntime is already owned by this factory")
        self._runtimes.append(_OwnedRuntime(runtime))
        return runtime

    async def close(self, runtime: MemoryRuntime) -> None:
        """Close one runtime early and release it from teardown ownership."""

        entry = self._find_entry(runtime)
        if entry is None:
            if self._is_closed(runtime):
                return
            raise ValueError("MemoryRuntime is not owned by this factory")
        task = self._ensure_close(entry)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and not task.cancelled() and task.exception() is None:
                self._retire(entry)
            raise
        else:
            self._retire(entry)

    async def close_all(self) -> None:
        """Close every remaining runtime in deterministic reverse order."""

        failures: list[BaseException] = []
        for entry in reversed(tuple(self._runtimes)):
            if not any(owned is entry for owned in self._runtimes):
                continue
            task = self._ensure_close(entry)
            failures.extend(await self._settle_close(task))
            if task.done() and not task.cancelled() and task.exception() is None:
                self._retire(entry)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            messages = "; ".join(
                f"{type(error).__name__}: {error}" for error in failures
            )
            raise RuntimeError(
                f"multiple MemoryRuntime close failures: {messages}"
            ) from failures[0]

    def _find_entry(self, runtime: MemoryRuntime) -> _OwnedRuntime | None:
        for entry in self._runtimes:
            if entry.runtime is runtime:
                return entry
        return None

    def _is_closed(self, runtime: MemoryRuntime) -> bool:
        return any(closed is runtime for closed in self._closed_runtimes)

    @staticmethod
    def _ensure_close(entry: _OwnedRuntime) -> asyncio.Task[None]:
        if entry.close_task is None:
            entry.close_task = asyncio.create_task(
                entry.runtime.close(),
                name="memory-runtime-test-close",
            )
        return entry.close_task

    @staticmethod
    async def _settle_close(task: asyncio.Task[None]) -> list[BaseException]:
        # Teardown cancellation must not abandon the one task that owns cleanup.
        failures: list[BaseException] = []
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if not task.cancelled():
                    failures.append(error)
            except BaseException:
                break
        try:
            task.result()
        except BaseException as error:
            failures.append(error)
        return failures

    def _retire(self, entry: _OwnedRuntime) -> None:
        for index, owned in enumerate(self._runtimes):
            if owned is entry:
                del self._runtimes[index]
                if not self._is_closed(entry.runtime):
                    self._closed_runtimes.append(entry.runtime)
                return


@asynccontextmanager
async def finalizing_memory_runtimes() -> AsyncIterator[MemoryRuntimeFactory]:
    owner = MemoryRuntimeFactory()
    try:
        yield owner
    finally:
        await owner.close_all()
