"""Typed read projection for the Memory Processing Record."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar

from core.memory.everos import ProviderHealthSnapshot
from core.memory.confined_filesystem import PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS
from core.memory.module import PROVIDER_READ_TIMEOUT_SECONDS
from core.memory.types import MemoryFailureLogEntry


SourceStatus = Literal["available", "partial", "stale", "unknown", "unavailable"]

# One composite read performs an opaque operator lookup, then reads both
# maintenance journals, then fans out over provider health, five local source
# surfaces, and maintenance metadata plus a freshness recheck. This is the
# controller work bound; the transport retains an additional fixed margin.
PROCESSING_RECORD_SQLITE_BOUND_SECONDS = PRIVATE_SQLITE_BUSY_TIMEOUT_SECONDS
PROCESSING_RECORD_JOURNAL_SURFACES = 2
PROCESSING_RECORD_SOURCE_SURFACES = 5
PROCESSING_RECORD_TRANSPORT_MARGIN_SECONDS = 5.0
PROCESSING_RECORD_WORK_TIMEOUT_SECONDS = (
    PROCESSING_RECORD_SQLITE_BOUND_SECONDS
    + PROCESSING_RECORD_JOURNAL_SURFACES * PROCESSING_RECORD_SQLITE_BOUND_SECONDS
    + max(
        PROVIDER_READ_TIMEOUT_SECONDS,
        PROCESSING_RECORD_SOURCE_SURFACES
        * PROCESSING_RECORD_SQLITE_BOUND_SECONDS,
        PROCESSING_RECORD_SQLITE_BOUND_SECONDS
        + PROCESSING_RECORD_JOURNAL_SURFACES
        * PROCESSING_RECORD_SQLITE_BOUND_SECONDS,
    )
)
PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS = (
    PROCESSING_RECORD_WORK_TIMEOUT_SECONDS
    + PROCESSING_RECORD_TRANSPORT_MARGIN_SECONDS
)


_AwaitResult = TypeVar("_AwaitResult")


@dataclass(frozen=True, slots=True)
class SourceObservation:
    status: SourceStatus
    observed_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessingSourceObservations:
    memcells: SourceObservation
    runs: SourceObservation
    semantic: SourceObservation


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
class MaintenanceObservation:
    block_reason: str | None
    can_delete_data: bool


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    data_exists: bool
    can_delete_data: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceProjection:
    source: SourceObservation
    data_exists: bool
    can_delete_data: bool


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

    resolve_operator: Callable[[str], Awaitable[str]]
    observe_maintenance: Callable[[str | None], Awaitable[MaintenanceObservation]]
    observe_health: Callable[[str | None], Awaitable[RuntimeHealthObservation]]
    failure_log: Callable[
        [str | None],
        Awaitable[FailureLogObservation],
    ]
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

    async def read_record(
        self,
        *,
        verified_user_key: str | None = None,
        operator_ref: str | None = None,
        deadline: float | None = None,
    ) -> ProcessingRecordSummary:
        """Read the complete Processing Record projection."""

        operator_ref, deadline = await self._prepare_authorized_read(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
            deadline=deadline,
        )
        return await self._read_record(operator_ref, deadline)

    async def read_status(
        self,
        *,
        deadline: float | None = None,
    ) -> RuntimeHealthProjection:
        """Read the narrow provider-only runtime status projection."""

        deadline = self._projection_deadline(
            deadline,
            timeout=PROVIDER_READ_TIMEOUT_SECONDS,
        )
        return await self._read_runtime(None, deadline)

    async def read_failures(
        self,
        *,
        verified_user_key: str | None = None,
        operator_ref: str | None = None,
        deadline: float | None = None,
    ) -> tuple[AnomalyProjection, MaintenanceProjection]:
        """Read failure entries and their owner-scoped recovery capability."""

        operator_ref, deadline = await self._prepare_authorized_read(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
            deadline=deadline,
        )
        return await self._read_failures(operator_ref, deadline)

    async def read_maintenance(
        self,
        *,
        verified_user_key: str | None = None,
        operator_ref: str | None = None,
        deadline: float | None = None,
    ) -> MaintenanceProjection:
        """Read owner-scoped maintenance facts."""

        operator_ref, deadline = await self._prepare_authorized_read(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
            deadline=deadline,
        )
        return await self._read_maintenance_projection(operator_ref, deadline)

    async def _prepare_authorized_read(
        self,
        *,
        verified_user_key: str | None,
        operator_ref: str | None,
        deadline: float | None,
    ) -> tuple[str | None, float]:
        deadline = self._projection_deadline(
            deadline,
            timeout=PROCESSING_RECORD_WORK_TIMEOUT_SECONDS,
        )
        operator_ref = await self._resolve_operator(
            verified_user_key,
            operator_ref,
            deadline,
        )
        return operator_ref, deadline

    @staticmethod
    def _projection_deadline(
        deadline: float | None,
        *,
        timeout: float,
    ) -> float:
        return time.monotonic() + timeout if deadline is None else deadline

    async def _resolve_operator(
        self,
        verified_user_key: str | None,
        operator_ref: str | None,
        deadline: float,
    ) -> str | None:
        if verified_user_key is None:
            return operator_ref
        if operator_ref is not None:
            raise ValueError("Memory read identity is already resolved")
        try:
            return await _await_before(
                deadline,
                lambda: self._runtime.resolve_operator(verified_user_key),
            )
        except Exception:
            return None

    async def _read_record(
        self,
        operator_ref: str | None,
        deadline: float,
    ) -> ProcessingRecordSummary:
        observation = await self._read_maintenance_observation(
            operator_ref,
            deadline,
        )
        runtime, sources, anomalies, maintenance = await asyncio.gather(
            self._read_runtime(observation.block_reason, deadline),
            self._read_sources(observation.block_reason, deadline),
            self._read_durable_anomalies(observation.block_reason, deadline),
            self._read_maintenance(operator_ref, observation, deadline),
        )
        return ProcessingRecordSummary(
            runtime=runtime,
            sources=sources,
            anomalies=anomalies,
            maintenance=maintenance,
        )

    async def _read_runtime(
        self,
        maintenance_reason: str | None,
        deadline: float,
    ) -> RuntimeHealthProjection:
        return await self._read_health(maintenance_reason, deadline)

    async def _read_failures(
        self,
        operator_ref: str | None,
        deadline: float,
    ) -> tuple[AnomalyProjection, MaintenanceProjection]:
        observation = await self._read_maintenance_observation(
            operator_ref,
            deadline,
        )
        anomalies, maintenance = await asyncio.gather(
            self._read_durable_anomalies(observation.block_reason, deadline),
            self._read_maintenance(operator_ref, observation, deadline),
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
        return anomalies, maintenance

    async def _read_maintenance_projection(
        self,
        operator_ref: str | None,
        deadline: float,
    ) -> MaintenanceProjection:
        observation = await self._read_maintenance_observation(
            operator_ref,
            deadline,
        )
        return await self._read_maintenance(
            operator_ref,
            observation,
            deadline,
        )

    async def _read_maintenance_observation(
        self,
        operator_ref: str | None,
        deadline: float,
    ) -> MaintenanceObservation:
        try:
            return await _await_before(
                deadline,
                lambda: self._runtime.observe_maintenance(operator_ref),
            )
        except Exception:
            return MaintenanceObservation(
                block_reason="memory_store_unavailable",
                can_delete_data=False,
            )

    async def _read_health(
        self,
        maintenance_reason: str | None,
        deadline: float,
    ) -> RuntimeHealthProjection:
        try:
            observation = await _await_before(
                deadline,
                lambda: self._runtime.observe_health(maintenance_reason),
            )
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
        deadline: float,
    ) -> ProcessingSourceObservations:
        try:
            return await _await_before(
                deadline,
                lambda: self._runtime.observe_sources(maintenance_reason),
            )
        except Exception:
            unavailable = SourceObservation(
                "unavailable",
                reason="memory_store_unavailable",
            )
            return ProcessingSourceObservations(
                memcells=unavailable,
                runs=unavailable,
                semantic=unavailable,
            )

    async def _read_durable_anomalies(
        self,
        maintenance_reason: str | None,
        deadline: float,
    ) -> AnomalyProjection:
        try:
            observation = await _await_before(
                deadline,
                lambda: self._runtime.failure_log(maintenance_reason),
            )
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

    async def _read_maintenance(
        self,
        operator_ref: str | None,
        observation: MaintenanceObservation,
        deadline: float,
    ) -> MaintenanceProjection:
        try:
            result = await _await_before(
                deadline,
                lambda: self._runtime.maintenance(operator_ref, observation),
            )
        except Exception:
            return MaintenanceProjection(
                source=SourceObservation(
                    "unavailable",
                    reason="memory_store_unavailable",
                ),
                data_exists=True,
                can_delete_data=False,
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
            can_delete_data=result.can_delete_data,
        )

def _utc_observed_at() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


async def _await_before(
    deadline: float,
    operation: Callable[[], Awaitable[_AwaitResult]],
) -> _AwaitResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(operation(), timeout=remaining)
