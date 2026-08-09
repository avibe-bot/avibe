"""Typed read projection for the Memory Processing Record."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from core.memory.everos import ProviderHealthSnapshot
from core.memory.maintenance import (
    ClearRecoveryResult,
    MaintenanceObservation,
    MaintenanceResult,
)
from core.memory.types import MemoryFailureLogEntry


SourceStatus = Literal["available", "partial", "stale", "unknown", "unavailable"]


@dataclass(frozen=True, slots=True)
class SourceObservation:
    status: SourceStatus
    observed_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessingSourceObservations:
    everos: SourceObservation
    capture: SourceObservation
    calls: SourceObservation


@dataclass(frozen=True, slots=True)
class RuntimeHealthObservation:
    snapshot: ProviderHealthSnapshot | None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeHealthProjection:
    source: SourceObservation
    health: ProviderHealthSnapshot | None


@dataclass(frozen=True, slots=True)
class AnomalyProjection:
    source: SourceObservation
    items: tuple[MemoryFailureLogEntry, ...]


@dataclass(frozen=True, slots=True)
class FailureLogObservation:
    items: tuple[MemoryFailureLogEntry, ...]
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceProjection:
    source: SourceObservation
    data_exists: bool
    can_clear: bool
    clear_recovery: ClearRecoveryResult | None


@dataclass(frozen=True, slots=True)
class ProcessingRecordSummary:
    runtime: RuntimeHealthProjection
    sources: ProcessingSourceObservations
    anomalies: AnomalyProjection
    maintenance: MaintenanceProjection
    status: Literal["ok"] = "ok"


@dataclass(frozen=True, slots=True)
class MemoryProcessingRecordPort:
    """Capability-shaped observations supplied by ``MemoryRuntime``."""

    observe_maintenance: Callable[[str | None], Awaitable[MaintenanceObservation]]
    observe_health: Callable[[str | None], Awaitable[RuntimeHealthObservation]]
    failure_log: Callable[
        [str | None],
        Awaitable[FailureLogObservation],
    ]
    recorder_health: Callable[[], Mapping[str, str | None]]
    observe_sources: Callable[[str | None], Awaitable[ProcessingSourceObservations]]
    maintenance: Callable[
        [str | None, MaintenanceObservation],
        Awaitable[MaintenanceResult],
    ]


class MemoryProcessingRecord:
    """Own Processing Record freshness, episodes, and source-local assembly."""

    def __init__(self, runtime: MemoryProcessingRecordPort) -> None:
        self._runtime = runtime
        self._last_health_snapshot: ProviderHealthSnapshot | None = None
        self._last_health_observed_at: str | None = None
        self._recorder_health: dict[str, str | None] = {
            "state": "disabled",
            "reason": None,
        }
        self._recorder_episode_observed_at: str | None = None
        self._recorder_episode_id: str | None = None

    async def read(self, operator_ref: str | None) -> ProcessingRecordSummary:
        """Read one independently degrading Processing Record summary."""

        observation = await self._read_maintenance_observation(operator_ref)
        runtime_result, sources, anomalies, maintenance = await asyncio.gather(
            self._read_runtime(observation),
            self._read_sources(observation.block_reason),
            self._read_durable_anomalies(observation.block_reason),
            self._read_maintenance(operator_ref, observation),
        )
        runtime, recorder_anomaly = runtime_result
        return ProcessingRecordSummary(
            runtime=runtime,
            sources=sources,
            anomalies=self._merge_recorder_anomaly(anomalies, recorder_anomaly),
            maintenance=maintenance,
        )

    async def read_runtime(
        self,
        observation: MaintenanceObservation | None = None,
    ) -> RuntimeHealthProjection:
        """Project runtime health without reading the durable data sources."""

        if observation is None:
            observation = await self._read_maintenance_observation(None)
        runtime, _recorder_anomaly = await self._read_runtime(observation)
        return runtime

    async def _read_runtime(
        self,
        observation: MaintenanceObservation,
    ) -> tuple[RuntimeHealthProjection, MemoryFailureLogEntry | None]:
        runtime = await self._read_health(observation.block_reason)
        self.observe_recorder(
            self._runtime.recorder_health(),
            observed_at=(
                runtime.source.observed_at
                if runtime.source.status == "available"
                else None
            ),
        )
        return runtime, self._recorder_anomaly()

    async def read_failures(
        self,
        operator_ref: str | None,
    ) -> tuple[AnomalyProjection, MaintenanceProjection]:
        """Project compatibility failure facts without probing runtime health."""

        self.observe_recorder(self._runtime.recorder_health())
        recorder_anomaly = self._recorder_anomaly()
        observation = await self._read_maintenance_observation(operator_ref)
        anomalies, maintenance = await asyncio.gather(
            self._read_durable_anomalies(observation.block_reason),
            self._read_maintenance(operator_ref, observation),
        )
        if (
            observation.block_reason is not None
            and anomalies.source.status == "unavailable"
            and anomalies.source.reason == observation.block_reason
        ):
            anomalies = AnomalyProjection(
                source=SourceObservation(
                    "unknown",
                    reason=observation.block_reason,
                ),
                items=anomalies.items,
            )
        return (
            self._merge_recorder_anomaly(anomalies, recorder_anomaly),
            maintenance,
        )

    async def read_maintenance(
        self,
        operator_ref: str | None,
    ) -> MaintenanceProjection:
        """Project compatibility maintenance facts without unrelated reads."""

        observation = await self._read_maintenance_observation(operator_ref)
        return await self._read_maintenance(operator_ref, observation)

    async def _read_maintenance_observation(
        self,
        operator_ref: str | None,
    ) -> MaintenanceObservation:
        try:
            return await self._runtime.observe_maintenance(operator_ref)
        except Exception:
            return MaintenanceObservation(
                block_reason="memory_store_unavailable",
                clear_recovery=None,
                can_clear=False,
            )

    async def _read_health(
        self,
        maintenance_reason: str | None,
    ) -> RuntimeHealthProjection:
        try:
            observation = await self._runtime.observe_health(maintenance_reason)
        except Exception:
            observation = RuntimeHealthObservation(
                snapshot=None,
                unavailable_reason="memory_sidecar_unavailable",
            )
        if observation.snapshot is not None:
            observed_at = _utc_observed_at()
            self._last_health_snapshot = observation.snapshot
            self._last_health_observed_at = observed_at
            return RuntimeHealthProjection(
                source=SourceObservation("available", observed_at=observed_at),
                health=observation.snapshot,
            )
        if self._last_health_snapshot is not None:
            return RuntimeHealthProjection(
                source=SourceObservation(
                    "stale",
                    observed_at=self._last_health_observed_at,
                    reason=observation.unavailable_reason,
                ),
                health=self._last_health_snapshot,
            )
        return RuntimeHealthProjection(
            source=SourceObservation(
                "unavailable",
                reason=observation.unavailable_reason,
            ),
            health=None,
        )

    async def _read_sources(
        self,
        maintenance_reason: str | None,
    ) -> ProcessingSourceObservations:
        try:
            return await self._runtime.observe_sources(maintenance_reason)
        except Exception:
            unavailable = SourceObservation(
                "unavailable",
                reason="memory_store_unavailable",
            )
            return ProcessingSourceObservations(
                everos=unavailable,
                capture=unavailable,
                calls=unavailable,
            )

    async def _read_durable_anomalies(
        self,
        maintenance_reason: str | None,
    ) -> AnomalyProjection:
        try:
            observation = await self._runtime.failure_log(maintenance_reason)
            durable = observation.items
            source = (
                SourceObservation(
                    "unavailable",
                    reason=observation.unavailable_reason,
                )
                if observation.unavailable_reason is not None
                else SourceObservation("available", observed_at=_utc_observed_at())
            )
        except Exception:
            durable = ()
            source = SourceObservation(
                "unavailable",
                reason="memory_store_unavailable",
            )
        return AnomalyProjection(source=source, items=durable[:50])

    def _merge_recorder_anomaly(
        self,
        durable: AnomalyProjection,
        recorder: MemoryFailureLogEntry | None,
    ) -> AnomalyProjection:
        items = list(durable.items)
        if recorder is not None:
            items.insert(0, recorder)
        return AnomalyProjection(source=durable.source, items=tuple(items[:50]))

    async def _read_maintenance(
        self,
        operator_ref: str | None,
        observation: MaintenanceObservation,
    ) -> MaintenanceProjection:
        try:
            result = await self._runtime.maintenance(operator_ref, observation)
        except Exception:
            return MaintenanceProjection(
                source=SourceObservation(
                    "unavailable",
                    reason="memory_store_unavailable",
                ),
                data_exists=True,
                can_clear=False,
                clear_recovery=None,
            )
        unavailable_reason = result.error
        if (
            unavailable_reason is None
            and observation.block_reason == "memory_store_unavailable"
        ):
            unavailable_reason = observation.block_reason
        source = (
            SourceObservation("unavailable", reason=unavailable_reason)
            if unavailable_reason is not None
            else SourceObservation("available", observed_at=_utc_observed_at())
        )
        return MaintenanceProjection(
            source=source,
            data_exists=result.data_exists,
            can_clear=result.can_clear,
            clear_recovery=result.clear_recovery,
        )

    def observe_recorder(
        self,
        health: Mapping[str, str | None],
        *,
        observed_at: str | None = None,
    ) -> None:
        previous = self._recorder_health
        current = dict(health)
        if current.get("state") != "degraded":
            self._recorder_episode_observed_at = None
            self._recorder_episode_id = None
        elif (
            previous.get("state") != "degraded"
            or previous.get("reason") != current.get("reason")
            or self._recorder_episode_observed_at is None
            or self._recorder_episode_id is None
        ):
            self._recorder_episode_observed_at = observed_at or _utc_observed_at()
            self._recorder_episode_id = _new_recorder_episode_id()
        self._recorder_health = current

    def _recorder_anomaly(self) -> MemoryFailureLogEntry | None:
        if (
            self._recorder_health.get("state") != "degraded"
            or self._recorder_episode_observed_at is None
            or self._recorder_episode_id is None
        ):
            return None
        return MemoryFailureLogEntry(
            id=self._recorder_episode_id,
            kind="recorder_degraded",
            state="degraded",
            operation="record",
            occurred_at=self._recorder_episode_observed_at,
            error_code="memory_processing_failed",
        )


def _new_recorder_episode_id() -> str:
    return f"ma_{secrets.token_hex(32)}"


def _utc_observed_at() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
