"""Finalizing ownership for test-created Memory runtimes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from config.v2_config import MemoryConfig
from core.memory.artifact import MemoryArtifactPort
from core.memory.everos_insight.reader import MemoryInsightReader
from core.memory.process import EverOSProcessFactory
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.worker import ProcessingEvent


class MemoryRuntimeFactory:
    """Construct and finalize explicitly configured runtimes for one test."""

    def __init__(self) -> None:
        self._runtimes: list[MemoryRuntime] = []

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
    ) -> MemoryRuntime:
        runtime = MemoryRuntime(
            config,
            store=store,
            artifact_manager=artifact_manager,
            process_factory=process_factory,
            effective_home=effective_home,
            processing_event=processing_event,
            insight_reader=insight_reader,
        )
        self._runtimes.append(runtime)
        return runtime

    def register(self, runtime: MemoryRuntime) -> MemoryRuntime:
        """Own a runtime constructed through the production factory under test."""

        if any(owned is runtime for owned in self._runtimes):
            raise ValueError("MemoryRuntime is already owned by this factory")
        self._runtimes.append(runtime)
        return runtime

    async def close(self, runtime: MemoryRuntime) -> None:
        """Close one runtime early and release it from teardown ownership."""

        index = self._owned_index(runtime)
        await runtime.close()
        del self._runtimes[index]

    async def close_all(self) -> None:
        """Close every remaining runtime in deterministic reverse order."""

        failures: list[BaseException] = []
        while self._runtimes:
            runtime = self._runtimes.pop()
            try:
                await runtime.close()
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            messages = "; ".join(
                f"{type(error).__name__}: {error}" for error in failures
            )
            raise RuntimeError(
                f"multiple MemoryRuntime close failures: {messages}"
            ) from failures[0]

    def _owned_index(self, runtime: MemoryRuntime) -> int:
        for index, owned in enumerate(self._runtimes):
            if owned is runtime:
                return index
        raise ValueError("MemoryRuntime is not owned by this factory")


@asynccontextmanager
async def finalizing_memory_runtimes() -> AsyncIterator[MemoryRuntimeFactory]:
    owner = MemoryRuntimeFactory()
    try:
        yield owner
    finally:
        await owner.close_all()
