"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import logging
import os
import stat
import time
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from config import paths
from config.v2_config import (
    MEMORY_RECOVERY_INTENTS,
    MemoryConfig,
    V2Config,
    atomic_update_memory,
)
from core.memory.artifact import (
    EVEROS_VERSION,
    MemoryArtifactCandidate,
    MemoryArtifactPort,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
    get_memory_artifact_manager,
)
from core.memory.blocking import run_blocking
from core.memory.clear_intent import ClearSurface
from core.memory.confined_filesystem import required_no_follow_flag
from core.memory.everos import (
    EverOSPort,
    MemoryProviderFailure,
    ProviderHealthSnapshot,
)
from core.memory.everos_insight import MemoryInsightPaths, MemoryInsightReader
from core.memory.everos_insight.recorder import clear_call_log, maintain_call_log, record_preflight_call
from core.memory.module import MemoryModule, MemorySessionLifecycleBusyError
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from core.memory.maintenance import (
    ClearInProgressResult,
    ClearResult,
    MaintenanceObservation,
    MaintenanceResult,
    MaintenanceRuntimeState,
    MemoryMaintenance,
    MemoryMaintenanceRuntimePort,
    MemoryStoreUnavailableError,
)
from core.memory.process import (
    EverOSProcess,
    EverOSProcessFactory,
    EverOSProcessPort,
    EverOSRebuildProcess,
    EverOSProcessSettings,
    RebuildProcessResult,
)
from core.memory.sidecar_lifecycle import MemorySidecarLifecycle, SidecarSnapshot
from core.memory.sync_process import EverOSSyncProcess, SyncProcessResult
from core.memory.processing_record import (
    AnomalyProjection,
    FailureLogObservation,
    MaintenanceProjection,
    MemoryProcessingRecord,
    MemoryProcessingRecordPort,
    ProcessingRecordSummary,
    ProcessingSourceObservations,
    ProviderCheckProjection,
    RuntimeHealthObservation,
    RuntimeHealthProjection,
    SourceObservation,
)
from core.memory.provider_root import (
    ProviderRoot,
    ProviderRootMetadata,
    ProviderRootRollback,
)
from core.memory.project_ids import (
    DEFAULT_MEMORY_PROJECT_ID,
    MEMORY_SEARCH_ALL_PROJECTS,
)
from core.memory.store import MemoryStore, is_principal_id, is_project_id
from core.memory.types import (
    MemoryFailureLogEntry,
    MemoryItem,
    MemoryItems,
    MemoryListPage,
    MemoryListItem,
    MemoryListResult,
    MemoryListWarningCode,
    MemoryResult,
    MemoryWarningCode,
    OperationFailed,
    RecallItems,
    RecallPolicy,
    RecallResult,
    memory_item_payload,
    memory_list_page_payload,
)
from core.memory.worker import ProcessingEvent


logger = logging.getLogger(__name__)


_SessionLifecycleResult = TypeVar("_SessionLifecycleResult")


ARTIFACT_ACTIVATION_TIMEOUT_SECONDS = 90.0
_CALL_LOG_RETENTION_INTERVAL_SECONDS = 6 * 60 * 60
_RECORDER_DISABLED = {"state": "disabled", "reason": None}
_RECORDER_DEGRADED = {"state": "degraded", "reason": "writer_failures"}
_MEMORY_LIST_CURSOR_VERSION = 3
MEMORY_LIST_CURSOR_MAX_BYTES = 8192
_MEMORY_LIST_PROVIDER_PAGE_SIZE = 20
_MEMORY_LIST_PROVIDER_MAX_PAGE = 1_000_000
_MEMORY_LIST_AGGREGATE_TIMEOUT_SECONDS = 20.0


@asynccontextmanager
async def _concurrent_episode_lists(
    module: MemoryModule,
    *,
    deadline: float,
) -> AsyncIterator[Callable[..., Awaitable[MemoryListResult]]]:
    batch = getattr(module, "concurrent_episode_lists", None)
    if batch is None:
        yield module.list_episodes
        return
    async with batch(deadline=deadline) as list_episodes:
        yield list_episodes


@dataclass(frozen=True, slots=True)
class _ProcessingRuntimeSnapshot:
    generation: int
    transition_active: bool
    enabled: bool
    store_available: bool
    maintenance_active: bool
    closing: bool
    process: EverOSProcessPort | None
    launch_token: int
    process_running: bool
    runtime_error: str | None

    def unavailable_reason(self, maintenance_reason: str | None) -> str | None:
        if not self.enabled:
            return "memory_disabled"
        if not self.store_available:
            return "memory_sidecar_unavailable"
        if maintenance_reason is not None:
            return maintenance_reason
        if self.maintenance_active or self.transition_active or self.closing:
            return "busy"
        if self.process is None or not self.process_running:
            return self.runtime_error or "memory_sidecar_unavailable"
        return None

    def local_observation_reason(
        self,
        maintenance_reason: str | None,
    ) -> str | None:
        """Gate local history reads independently of provider liveness."""

        if not self.store_available:
            return "memory_store_unavailable"
        if maintenance_reason is not None:
            return maintenance_reason
        if self.maintenance_active or self.transition_active or self.closing:
            return "busy"
        return None

    def same_lifecycle(self, other: _ProcessingRuntimeSnapshot) -> bool:
        return (
            self.generation == other.generation
            and self.transition_active == other.transition_active
            and self.enabled == other.enabled
            and self.store_available == other.store_available
            and self.maintenance_active == other.maintenance_active
            and self.closing == other.closing
            and self.process is other.process
            and self.launch_token == other.launch_token
            and self.process_running == other.process_running
            and self.runtime_error == other.runtime_error
        )


class _LifecycleGenerationLock:
    """Publish reconcile transitions without making health readers wait."""

    def __init__(self, on_transition: Callable[[], None]) -> None:
        self._lock = asyncio.Lock()
        self._on_transition = on_transition

    async def acquire(self) -> bool:
        acquired = await self._lock.acquire()
        self._on_transition()
        return acquired

    def release(self) -> None:
        self._lock.release()
        self._on_transition()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> _LifecycleGenerationLock:
        await self.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


def _clear_in_progress_payload(
    recovery: ClearInProgressResult | None,
) -> dict[str, Any] | None:
    if recovery is None:
        return None
    return {
        "state": recovery.state,
        "operation_id": recovery.operation_id,
        "occurred_at": recovery.occurred_at,
        "error_code": recovery.error_code,
    }


def _clear_result_payload(result: ClearResult) -> dict[str, Any]:
    if result.status == "failed":
        return {
            "status": "failed",
            "error": result.error,
            "clear_in_progress": _clear_in_progress_payload(result.clear_in_progress),
        }
    return {
        "status": result.status,
        "operation_id": result.operation_id,
        "epoch": result.epoch,
    }


def _source_observation_payload(source: SourceObservation) -> dict[str, Any]:
    return {
        "status": source.status,
        "observed_at": source.observed_at,
        "reason": source.reason,
    }


def _runtime_health_payload(result: RuntimeHealthProjection) -> dict[str, Any]:
    health = None if result.health is None else result.health.payload()
    if health is not None and health.get("cascade") is None:
        health["cascade"] = {}
    return {
        "status": "ok",
        "source": _source_observation_payload(result.source),
        "health": health,
    }


def _anomaly_projection_payload(result: AnomalyProjection) -> dict[str, Any]:
    return {
        "source": _source_observation_payload(result.source),
        "items": [asdict(entry) for entry in result.items],
    }


def _maintenance_projection_payload(
    result: MaintenanceProjection,
) -> dict[str, Any]:
    return {
        "source": _source_observation_payload(result.source),
        "data_exists": result.data_exists,
        "can_clear": result.can_clear,
        "clear_in_progress": _clear_in_progress_payload(result.clear_in_progress),
    }


def _processing_record_payload(summary: ProcessingRecordSummary) -> dict[str, Any]:
    runtime = _runtime_health_payload(summary.runtime)
    return {
        "status": summary.status,
        "runtime": {
            "source": runtime["source"],
            "health": runtime["health"],
        },
        "sources": {
            "everos": _source_observation_payload(summary.sources.everos),
            "capture": _source_observation_payload(summary.sources.capture),
            "calls": _source_observation_payload(summary.sources.calls),
        },
        "anomalies": _anomaly_projection_payload(summary.anomalies),
        "maintenance": _maintenance_projection_payload(summary.maintenance),
        "provider_checks": {
            "source": _source_observation_payload(summary.provider_checks.source),
            "items": list(summary.provider_checks.items),
        },
    }


class _UnavailableMemoryModule:
    """Capture sink for a runtime whose store could not be opened.

    ``MemoryRuntime.module`` is only ever used to capture, so this narrow adapter
    is the whole seam, not a stand-in for the complete ``MemoryModule`` interface.
    """

    async def capture(self, _request: Any) -> OperationFailed:
        return OperationFailed(error="memory_store_unavailable")


_UNAVAILABLE_MODULE = _UnavailableMemoryModule()


def create_memory_runtime(
    config: MemoryConfig,
    *,
    artifact_manager: MemoryArtifactPort | None = None,
    process_factory: EverOSProcessFactory | None = None,
    effective_home: Path | None = None,
    processing_event: ProcessingEvent | None = None,
    insight_reader: MemoryInsightReader | None = None,
    on_config_settled: Callable[[MemoryConfig], None] | None = None,
) -> MemoryRuntime:
    """Construct Memory without allowing store failures to stop Avibe.

    Kept as the controller's entry point, but it no longer chooses between two
    classes: ``MemoryRuntime`` absorbs an unopenable store as one of its states.
    """

    return MemoryRuntime(
        config,
        artifact_manager=artifact_manager,
        process_factory=process_factory,
        effective_home=effective_home,
        processing_event=processing_event,
        insight_reader=insight_reader,
        on_config_settled=on_config_settled,
    )


class MemoryRuntime:
    """Own local Memory state, sidecar reconciliation, and periodic draining."""

    def __init__(
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
    ) -> None:
        self._config = config
        self._restart_config = deepcopy(config)
        self._effective_home = effective_home or paths.get_vibe_remote_dir()
        self._maintenance: MemoryMaintenance | None = None
        self._artifact_manager: MemoryArtifactPort = artifact_manager or get_memory_artifact_manager()
        self._provider_root_owner = ProviderRoot(
            self._provider_root,
            effective_home=self._effective_home,
        )
        self._process_factory: EverOSProcessFactory = process_factory or EverOSProcess
        self._processing_event = processing_event
        self._on_config_settled = on_config_settled
        # The controller-side port only talks to the private UDS. Credentials
        # enter an EverOSPort only inside the owned child probe/sidecar.
        self._provider = EverOSPort(self._socket_path)
        self._runtime_error: str | None = None
        self._processing_lifecycle_generation = 0
        self._reconcile_lock = _LifecycleGenerationLock(
            self._advance_processing_lifecycle
        )
        self._restart_task: asyncio.Task[dict[str, Any]] | None = None
        self._rebuild_task: asyncio.Task[dict[str, Any]] | None = None
        self._repair_task: asyncio.Task[dict[str, Any]] | None = None
        self._ready_activation_task: asyncio.Task[None] | None = None
        self._artifact_activation_task: asyncio.Task[None] | None = None
        self._closing = False
        self._worker_task: asyncio.Task[None] | None = None
        self._activation_loop: asyncio.AbstractEventLoop | None = None
        self._artifact_installing = False
        self._retired = False
        self._closed = False
        self._store: MemoryStore | None = None
        self._module: MemoryModule | None = None
        self._store_error: Exception | None = None
        self._insight_reader_override = insight_reader
        self._insight_reader: MemoryInsightReader | None = None
        self._recorder_health: dict[str, str | None] = dict(_RECORDER_DISABLED)
        self._sidecar = MemorySidecarLifecycle(
            self._process_factory,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            socket_path=self._socket_path,
            call_log_db_path=self._call_log_db_path,
            retain_call_log=self._maintain_call_log_once,
            on_current_sidecar_ready=self._current_sidecar_ready,
            on_recorder_health=self._update_recorder_health,
            read_recorder_health=self._provider.recorder_health,
        )
        self._maintenance = MemoryMaintenance(
            None,
            effective_home=self._effective_home,
            runtime=self._maintenance_runtime_port(),
        )
        self._processing_record = MemoryProcessingRecord(
            self._processing_record_port()
        )
        self._open_store(store)

    def _open_store(self, store: MemoryStore | None = None) -> bool:
        """Open the local store and build the module, absorbing any failure.

        A store Avibe cannot open must not stop the rest of the service, so this
        never raises. It records the cause instead and leaves the runtime in its
        unavailable state, which every public method already reports.
        """

        if self._module is not None:
            return True
        try:
            required_no_follow_flag()
            opened = store or MemoryStore()
            self._artifact_manager.set_provider_root(self._provider_root)
            module = MemoryModule(
                opened,
                self._provider,
                enabled=lambda: self._config.enabled,
                provider_root=self._provider_root,
                provider_root_owner=self._provider_root_owner,
                maintenance_open=self._maintenance_open,
                processing_event=self._processing_event,
                effective_home=self._effective_home,
            )
        except Exception as exc:
            self._store_error = exc
            logger.exception("Memory store initialization failed; continuing with Memory unavailable")
            return False
        self._store = opened
        self._module = module
        self._require_maintenance().attach_store(opened)
        if self._maintenance_open() or self.recovery_pending:
            module.pause_claims()
        self._store_error = None
        self._configure_insight_reader(self._config)
        self._artifact_manager.set_activation_coordinator(self._coordinate_artifact_activation)
        return True

    @property
    def available(self) -> bool:
        """Whether the local store opened. False keeps every read closed."""

        return self._module is not None and not self._retired

    @property
    def recovery_pending(self) -> bool:
        """Whether this aggregate is fenced by any durable recovery intent."""

        return any(
            getattr(config, "recovery_intent", None) in MEMORY_RECOVERY_INTENTS
            for config in (self._config, self._restart_config)
        )

    def _durable_recovery_intent(self) -> str | None:
        """Return the persisted recovery intent matching this runtime fence."""

        try:
            durable_intent = V2Config.load().memory.recovery_intent
        except Exception:
            logger.exception("Memory artifact admission could not load durable recovery intent")
            return None
        if durable_intent not in MEMORY_RECOVERY_INTENTS:
            return None
        if not any(
            config.recovery_intent == durable_intent
            for config in (self._config, self._restart_config)
        ):
            return None
        return durable_intent

    @property
    def factory_reset_pending(self) -> bool:
        """Whether this aggregate is fenced by a durable factory-reset intent."""

        return any(
            getattr(config, "recovery_intent", None) == "factory_reset"
            for config in (self._config, self._restart_config)
        )

    @property
    def rebuild_pending(self) -> bool:
        """Whether this aggregate is fenced by a durable embedding rebuild intent."""

        return any(
            getattr(config, "recovery_intent", None) == "rebuild"
            for config in (self._config, self._restart_config)
        )

    @property
    def retired(self) -> bool:
        """Whether this aggregate has been permanently retired."""

        return self._retired

    @property
    def closed(self) -> bool:
        """Whether the last close attempt completed all cleanup steps."""

        return self._closed

    @property
    def effective_home(self) -> Path:
        """Return the pinned home used by this aggregate's mutable state."""

        return self._effective_home

    @property
    def artifact_manager(self) -> MemoryArtifactPort:
        return self._artifact_manager

    @property
    def process_factory(self) -> EverOSProcessFactory:
        return self._process_factory

    def retire(self) -> None:
        """Tombstone this aggregate and its module before mutable roots die."""

        self._retired = True
        self._closing = True
        self._sidecar.close_ready_admission()
        if self._module is not None:
            self._module.retire()
        self._advance_processing_lifecycle()

    def artifact_admitted(self) -> bool:
        """Return whether the pinned artifact is valid for a reset admission."""

        try:
            status = self._artifact_manager.status()
            return (
                self._artifact_manager.resolve_python() is not None
                and status.get("status") == "ready"
                and status.get("reason") is None
            )
        except Exception:
            return False

    def adopt_recovery_intent(self, config: MemoryConfig) -> None:
        """Publish a durable recovery candidate and fence all module claims."""

        self._config = deepcopy(config)
        self._restart_config = deepcopy(config)
        if self.available and config.recovery_intent is not None:
            self.module.pause_claims()

    async def activate_fresh(self, config: MemoryConfig) -> dict[str, Any]:
        """Activate a newly constructed aggregate without re-reading old intent."""

        if self._retired:
            return {"ok": False, "error": "memory_operation_in_progress"}
        if not self.available:
            return {"ok": False, "error": "memory_store_unavailable"}
        self._activation_loop = asyncio.get_running_loop()
        self.module.pause_claims()
        async with self._reconcile_lock:
            async with self.module.lifecycle():
                result = await self._reconcile_locked(
                    config,
                    claims_already_paused=True,
                    skip_embedding_guard=True,
                    resume_claims_on_failure=False,
                )
                if result.get("ok") is True:
                    # Fresh runtimes start fenced while the child is proved;
                    # settled recovery must leave ordinary capture admission open.
                    self.module.resume_claims()
                return result

    async def retain_factory_reset_recovery(self, config: MemoryConfig) -> None:
        """Keep a failed fresh aggregate available for pending-reset repair.

        Factory reset has already removed the mutable roots at this point. A
        failed activation therefore must stop any partially-started sidecar and
        leave the aggregate fenced, but it must not tombstone the object: the
        Dependencies Repair path needs its artifact coordinator and store.
        """

        if self._retired:
            return
        async with self._reconcile_lock:
            async with self.module.lifecycle():
                self.module.pause_claims()
                await self._stop_worker()
                await self._sidecar.stop()
                self.adopt_recovery_intent(config)
                self._runtime_error = "memory_factory_reset_failed"
                self._advance_processing_lifecycle()

    @property
    def module(self) -> MemoryModule | _UnavailableMemoryModule:
        return self._module if self._module is not None else _UNAVAILABLE_MODULE

    def _unavailable(self) -> MemoryStoreUnavailableError:
        """Build the error the read surfaces raise, chained to the real cause."""

        error = MemoryStoreUnavailableError("Memory store is unavailable")
        error.__cause__ = self._store_error
        return error

    @property
    def _memory_dir(self) -> Path:
        return self._effective_home / "memory"

    @property
    def _provider_root(self) -> Path:
        return self._memory_dir / "everos-root"

    @property
    def _socket_path(self) -> Path:
        return self._memory_dir / ".rt" / "everos.sock"

    @property
    def _call_log_db_path(self) -> Path:
        return self._memory_dir / "call-log" / "call-log.db"

    # Remaining Runtime paths still read these projections while their own
    # lifecycle work is migrated. Ownership remains in ``_sidecar``.
    @property
    def _process(self) -> EverOSProcessPort | None:
        return self._sidecar.snapshot().process

    @_process.setter
    def _process(self, process: EverOSProcessPort | None) -> None:
        self._sidecar._replace_for_runtime(process)

    @property
    def _process_records_calls(self) -> bool:
        return self._sidecar.snapshot().records_calls

    @_process_records_calls.setter
    def _process_records_calls(self, value: bool) -> None:
        self._sidecar._set_records_calls_for_runtime(value)

    @property
    def _call_log_retention_task(self) -> asyncio.Task[None] | None:
        return self._sidecar.retention_task

    def _maintenance_open(self) -> bool:
        """Fail closed when the independent clear authority cannot prove terminal."""

        maintenance = self._maintenance
        return maintenance is None or maintenance.is_open()

    def _can_disable_without_maintenance_authority(self, config: MemoryConfig) -> bool:
        """Allow only a pure disable when the journal authority is unreadable."""

        if config.enabled or config != replace(self._config, enabled=False):
            return False
        maintenance = self._maintenance
        return maintenance is None or maintenance.can_disable_without_authority()

    def _maintenance_runtime_port(self) -> MemoryMaintenanceRuntimePort:
        return MemoryMaintenanceRuntimePort(
            exclusive_fence=self._maintenance_fence,
            boot_recovery_fence=self._maintenance_boot_recovery_fence,
            state=self._maintenance_runtime_state,
            enter_maintenance=self._enter_maintenance,
            leave_maintenance=self._leave_maintenance,
            pause_claims=self._pause_clear_claims,
            resume_claims=self._resume_maintenance_claims,
            quiesce=self._quiesce_for_clear,
            resume=self._resume_after_clear,
            delete_surface=self._delete_clear_surface,
            restore_completed=self._sidecar.reset_host_retention_after_clear,
        )

    def _processing_record_port(self) -> MemoryProcessingRecordPort:
        return MemoryProcessingRecordPort(
            resolve_operator=self._processing_record_operator,
            observe_maintenance=self._processing_record_maintenance_observation,
            observe_health=self._processing_record_health,
            failure_log=self._processing_record_failure_log,
            recorder_health=lambda: dict(self._recorder_health),
            observe_sources=self._processing_record_sources,
            provider_checks=self._processing_record_provider_checks,
            maintenance=self._processing_record_maintenance,
        )

    async def _processing_record_operator(self, user_key: str) -> str:
        if not self.available:
            raise self._unavailable()
        return await run_blocking(self._store.principal_for_user_key, user_key)

    def _advance_processing_lifecycle(self) -> None:
        self._processing_lifecycle_generation += 1

    def _processing_runtime_snapshot(self) -> _ProcessingRuntimeSnapshot:
        sidecar = self._sidecar.snapshot()
        process = sidecar.process
        module = self._module
        return _ProcessingRuntimeSnapshot(
            generation=self._processing_lifecycle_generation,
            transition_active=self._reconcile_lock.locked(),
            enabled=self._config.enabled,
            store_available=module is not None,
            maintenance_active=bool(module and module.maintenance_active),
            closing=self._closing,
            process=process,
            launch_token=sidecar.launch_token,
            process_running=bool(process and process.running),
            runtime_error=self._runtime_error,
        )

    @asynccontextmanager
    async def _maintenance_fence(self) -> AsyncIterator[None]:
        """Acquire destructive fences in their single permitted order."""

        async with self._reconcile_lock, self.module.destructive_lifecycle():
            yield

    @asynccontextmanager
    async def _maintenance_boot_recovery_fence(self) -> AsyncIterator[None]:
        """Add the root fence while reconcile and module fences are already held."""

        async with self.module.provider_root_lifecycle():
            yield

    def _maintenance_runtime_state(self) -> MaintenanceRuntimeState:
        return MaintenanceRuntimeState(
            artifact_installing=self._artifact_installing,
        )

    def _enter_maintenance(self) -> None:
        if self._module is not None:
            self._module.enter_maintenance()
        self._advance_processing_lifecycle()

    def _leave_maintenance(self) -> None:
        if self._module is not None:
            self._module.leave_maintenance()
        self._advance_processing_lifecycle()

    def _resume_maintenance_claims(self) -> None:
        if self._module is not None:
            self._module.resume_claims()

    def _configure_insight_reader(self, config: MemoryConfig) -> None:
        if self._insight_reader_override is not None:
            self._insight_reader = self._insight_reader_override
            return
        if self._store is None:
            self._insight_reader = None
            return
        rerank = config.processing.rerank
        base_urls = tuple(
            value
            for value in (
                config.processing.llm.base_url,
                config.processing.embedding.base_url,
                rerank.base_url if rerank else None,
            )
            if value
        )
        exact_redaction_values = tuple(
            value
            for value in (
                config.processing.llm.api_key,
                config.processing.embedding.api_key,
                rerank.api_key if rerank else None,
            )
            if value
        )
        self._insight_reader = MemoryInsightReader(
            MemoryInsightPaths(
                self._provider_root,
                self._store.path,
                self._call_log_db_path,
            ),
            provider_base_urls=base_urls,
            exact_redaction_values=exact_redaction_values,
        )

    async def _reap_recorded_sidecar_if_unowned(self, *, fail_closed: bool = False) -> bool:
        """Reap a previous run's sidecar when this runtime supervises none.

        ``EverOSProcess`` reaps a recorded orphan on its way to spawning a
        replacement, which covers every boot that gets as far as spawning one. It
        is the boots that do not that leave an orphan running forever: Memory
        persisted as disabled, a runtime artifact that will not resolve, a
        credential probe that fails, a store that will not open. None of those
        constructs a supervisor, so none of them used to reach the reap.

        This takes ``_reconcile_lock`` itself, as its own acquisition released
        before the reconciliation below takes it again -- sequential, never
        nested, so never call this while already holding that lock. The lock is
        what keeps a reap and a launch from overlapping: a reap runs for up to
        two stop-timeout rounds and retires the record when it finishes, so an
        unserialized one could delete a record a concurrent launch had written in
        the meantime and leave that live child untracked -- the very state an
        unwritable record is already made to fail a start over.

        Holding it also upgrades the self-reap guard from an argument about
        await points to plain mutual exclusion: under this lock,
        ``self._process is None`` means no child of ours exists and none can
        appear before the reap finishes. The record names a child of ours only
        from inside ``_start_locked``, and ``_reconcile_locked`` assigns the
        supervisor before calling ``start``, so a runtime that holds a child --
        starting, running, or retained after a failed cleanup -- never gets here.
        The claim rules do the rest: the short-lived processing probe carries our
        environment but not our ``--uds``, and our own pid is excluded outright.

        A reap that cannot finish does not fail the reconcile. On the disabled
        path there is no replacement to protect: nothing else will touch the
        provider root, and refusing to apply a disable the user has already saved
        would report a failure they cannot act on. On the enabled path the launch
        runs the same reap again moments later and fails closed there, so the
        guarantee is kept where it means something. Either way the record is
        retained, so the recovery stays available to the next attempt. A caller
        that is about to delete the provider root may pass ``fail_closed=True``
        to stop on that recovery failure instead.
        """

        async with self._reconcile_lock:
            if self._process is not None:
                return False
            try:
                required_no_follow_flag()
                recovery = EverOSRebuildProcess(
                    None,
                    effective_home=self._effective_home,
                    provider_root=self._provider_root,
                )
                await recovery.reconcile_orphan()
            except Exception as exc:
                logger.warning("Recorded EverOS sidecar recovery did not finish: %s", exc)
                if fail_closed:
                    raise
                return False
            return True

    async def _reap_recorded_sync_if_unowned(self, *, fail_closed: bool = False) -> bool:
        """Retire only an authenticated sync orphan from a prior controller."""

        try:
            required_no_follow_flag()
            recovery = EverOSSyncProcess(
                None,
                effective_home=self._effective_home,
                provider_root=self._provider_root,
            )
            await recovery.reconcile_orphan()
        except Exception as exc:
            # A live owner or unprovable record must not block sidecar boot.
            # The record remains fail-closed for a later Repair admission.
            logger.warning("Recorded EverOS sync recovery did not finish: %s", exc)
            if fail_closed:
                raise
            return False
        return True

    async def reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Apply persisted config without restarting the Avibe service."""

        if self._retired:
            return {"ok": False, "error": "memory_operation_in_progress"}
        try:
            return await self._reconcile(config)
        finally:
            if self._maintenance is not None:
                self._maintenance.ensure_housekeeping()

    async def _reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Run one reconciliation owned by the public lifecycle operation."""

        if self._exclusive_operation_running():
            return {"ok": False, "error": "memory_operation_in_progress"}

        # Sync ownership is independent of the sidecar/rebuild record. Retire a
        # prior controller's orphan before ordinary boot reconciliation, without
        # taking the provider-root lifecycle lock or touching a healthy sidecar.
        await self._reap_recorded_sync_if_unowned()

        # Before every early return below, because a boot that never launches a
        # sidecar is exactly the boot that may face one from the run before it.
        # Takes and releases the reconcile lock itself; the lock this method
        # acquires later is a separate, sequential acquisition.
        recorded_sidecar_reaped = await self._reap_recorded_sidecar_if_unowned()
        if recorded_sidecar_reaped and not self._maintenance_open():
            # Retention may run while artifact/store/credential preflight is
            # failing, but only after recovery proved no previous recorder can
            # still own the database. A recorder launch fences this task again
            # through ``before_recorder_start`` below.
            self._ensure_call_log_retention()
        if not self.available:
            # A transient store failure must not close Memory forever: every
            # reconciliation is another chance to open it.
            self._config = config
            if not config.enabled:
                async with self._reconcile_lock:
                    self._restart_config = deepcopy(config)
                    return {"ok": True, "state": "disabled"}
            if not self._open_store():
                logger.warning("Memory store remains unavailable during reconciliation")
                return {"ok": False, "error": "memory_store_unavailable"}
        async with self._reconcile_lock:
            # A rebuild may have finished while this call reaped an orphan.
            # Recheck ownership and prefer the durable unit so a stale pending
            # snapshot cannot stop a just-activated sidecar.
            if self._exclusive_operation_running():
                return {"ok": False, "error": "memory_operation_in_progress"}
            try:
                durable = await asyncio.to_thread(lambda: V2Config.load().memory)
            except Exception:
                durable = None
            if durable is not None and config != durable:
                config = deepcopy(durable)
            self._activation_loop = asyncio.get_running_loop()
            if self._artifact_installing:
                return {"ok": False, "error": "memory_runtime_install_failed"}
            # This is deliberately the same lifecycle lock Clear uses. A settings
            # save cannot race a root wipe or replace sidecar credentials halfway
            # through an active provider call.
            async with self.module.lifecycle():
                maintenance_open = self._maintenance_open()
                if (
                    maintenance_open
                    and self._can_disable_without_maintenance_authority(config)
                ):
                    result = await self._disable_locked(config)
                elif (
                    maintenance_open
                    and self._maintenance is not None
                    and self._maintenance.has_readable_intent()
                ):
                    result = await self._reconcile_locked(config)
                elif maintenance_open:
                    self.module.pause_claims()
                    return {"ok": False, "error": "memory_clear_failed"}
                else:
                    result = await self._reconcile_locked(config)
                if result.get("ok") is True:
                    self._restart_config = deepcopy(config)
                return result

    async def _disable_locked(self, config: MemoryConfig) -> dict[str, Any]:
        """Stop every active Memory component without consulting maintenance state."""

        self.module.pause_claims()
        self._config = config
        self._configure_insight_reader(config)
        self._provider = EverOSPort(self._socket_path)
        self.module.replace_provider(self._provider)
        await self._stop_worker()
        stopped_process = self._process is not None
        if stopped_process:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False
        self._reset_recorder_health_unless_corrupt()
        if stopped_process:
            self._ensure_call_log_retention()
        self._runtime_error = None
        return {"ok": True, "state": "disabled"}

    async def _reconcile_locked(
        self,
        config: MemoryConfig,
        *,
        claims_already_paused: bool = False,
        skip_embedding_guard: bool = False,
        resume_claims_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Reconcile while both controller and module lifecycle locks are held."""

        if self._maintenance is not None and self._maintenance.is_open():
            lease = MemoryOperationLease(self._effective_home)
            try:
                try:
                    await run_blocking(lease.acquire)
                except MemoryOperationBusy:
                    self.module.pause_claims()
                    self._runtime_error = "memory_operation_in_progress"
                    return {"ok": False, "error": self._runtime_error}
                recovered = await self._maintenance.recover_boot(lease_held=True)
            finally:
                await run_blocking(lease.release)
            if not recovered:
                self.module.pause_claims()
                self._runtime_error = "memory_clear_failed"
                return {"ok": False, "error": self._runtime_error}

        if self._maintenance_open():
            self.module.pause_claims()
            if self._can_disable_without_maintenance_authority(config):
                return await self._disable_locked(config)
            return {"ok": False, "error": "memory_clear_failed"}

        if config.recovery_intent == "rebuild":
            # A durable recovery intent is the crash-safe retry state. Startup
            # and ordinary reconcile only fence the candidate; destructive work
            # is entered explicitly through ``rebuild()``.
            try:
                quiesced = claims_already_paused or await self.module.quiesce_claims()
            except Exception:
                quiesced = False
            if not quiesced:
                self._runtime_error = "memory_rebuild_failed"
                return {"ok": False, "error": self._runtime_error}
            self.module.pause_claims()
            await self._stop_worker()
            await self._sidecar.stop()
            self._config = deepcopy(config)
            self._restart_config = deepcopy(config)
            self._runtime_error = "memory_embedding_rebuild_required"
            return {"ok": False, "error": self._runtime_error}

        if config.recovery_intent == "factory_reset":
            self.module.pause_claims()
            await self._stop_worker()
            await self._sidecar.stop()
            self._config = deepcopy(config)
            self._restart_config = deepcopy(config)
            self._runtime_error = "memory_factory_reset_failed"
            return {"ok": False, "error": self._runtime_error}

        embedding_changed = (
            not skip_embedding_guard
            and _embedding_configuration_changed(self._config, config)
        )
        claims_paused = claims_already_paused
        if embedding_changed:
            # Stop the worker before inspecting provider state. A capture may
            # still enqueue while settings are being reconciled, but no
            # old-embedding drain can cross this boundary.
            if not await self.module.quiesce_claims():
                # ``pause_and_wait`` fences claims before waiting, so a timeout
                # leaves them fenced. Releasing here is what keeps a failed
                # settings save from silently stopping the drain loop forever.
                if resume_claims_on_failure:
                    self.module.resume_claims()
                self._runtime_error = "memory_clear_failed"
                return {"ok": False, "error": self._runtime_error}
            claims_paused = True
            embedding_guard_rejected = False
            try:
                if await asyncio.to_thread(self._provider_data_exists_strict):
                    embedding_guard_rejected = True
                    self._runtime_error = "memory_clear_failed"
                    return {"ok": False, "error": self._runtime_error}
            except Exception:
                # An indeterminate root/queue state cannot safely accept an
                # embedding change because it could mix vector spaces.
                embedding_guard_rejected = True
                self._runtime_error = "memory_clear_failed"
                return {"ok": False, "error": self._runtime_error}
            finally:
                if embedding_guard_rejected and resume_claims_on_failure:
                    self.module.resume_claims()
                    claims_paused = False

        if not config.enabled:
            return await self._disable_locked(config)

        # Preflight before touching a healthy child. This keeps an active
        # configuration alive when a replacement endpoint or runtime is
        # unavailable, and keeps credentials out of the UI process.
        candidate_provider = EverOSPort(
            self._socket_path,
            processing_health_check=self._processing_healthy,
        )
        python = await asyncio.to_thread(self._artifact_manager.resolve_python)
        if python is None:
            error = _runtime_error_for_status(await asyncio.to_thread(self._artifact_manager.status))
            if not (self._process and self._process.running):
                self._runtime_error = error
            if claims_paused and resume_claims_on_failure:
                self.module.resume_claims()
            return {"ok": False, "error": error}
        if not await self._probe_processing(python, config):
            error = "memory_processing_failed"
            if not (self._process and self._process.running):
                self._runtime_error = error
            if claims_paused and resume_claims_on_failure:
                self.module.resume_claims()
            return {"ok": False, "error": error}

        # Every enabled reconciliation receives a fresh process. Endpoint,
        # model, and key changes belong exclusively in its allowlisted child
        # environment and must never leave an old sidecar running.
        if not claims_paused and not await self.module.quiesce_claims():
            # Same fence release as the embedding guard above: a timed-out pause
            # must not leave the worker permanently unable to claim.
            if resume_claims_on_failure:
                self.module.resume_claims()
            self._runtime_error = "memory_clear_failed"
            return {"ok": False, "error": self._runtime_error}
        await self._stop_worker()
        await self._sidecar.stop()

        self._config = config
        self._configure_insight_reader(config)
        self._provider = candidate_provider
        self.module.replace_provider(self._provider)
        try:
            meta = await asyncio.to_thread(self._store.ensure_meta)
            await run_blocking(
                self._provider_root_owner.ensure,
                meta,
                self._active_provider_root_metadata(),
            )
        except Exception:
            self._runtime_error = "memory_clear_failed"
            if resume_claims_on_failure:
                self.module.resume_claims()
            return {"ok": False, "error": self._runtime_error}

        settings = _process_settings(
            config,
            call_log_db_path=self._call_log_db_path,
        )
        try:
            started = await self._sidecar.start(python, settings)
        except BaseException:
            raise
        if not started:
            self._runtime_error = "memory_sidecar_unavailable"
            return {"ok": False, "error": self._runtime_error}
        self._runtime_error = None
        self.module.resume_claims()
        self._ensure_worker()
        return {"ok": True, "state": "ready"}

    async def _processing_record_maintenance_observation(
        self,
        operator_ref: str | None,
    ) -> MaintenanceObservation:
        maintenance = self._maintenance
        if maintenance is None:
            return MaintenanceObservation(
                block_reason="memory_store_unavailable",
                clear_in_progress=None,
                can_clear=False,
            )
        return await maintenance.observe(operator_ref=operator_ref)

    async def _processing_record_health(
        self,
        maintenance_reason: str | None,
    ) -> RuntimeHealthObservation:
        snapshot: ProviderHealthSnapshot | None = None
        reason: str | None = None
        before = self._processing_runtime_snapshot()
        reason = before.unavailable_reason(maintenance_reason)
        if reason is not None:
            return RuntimeHealthObservation(snapshot=None, unavailable_reason=reason)
        try:
            snapshot = await self._provider.health_snapshot()
        except MemoryProviderFailure as failure:
            reason = failure.error
        except Exception:
            reason = "memory_sidecar_unavailable"
        after = self._processing_runtime_snapshot()
        current_reason = after.unavailable_reason(maintenance_reason)
        if not after.same_lifecycle(before) or current_reason is not None:
            return RuntimeHealthObservation(
                snapshot=None,
                unavailable_reason=current_reason or "memory_sidecar_unavailable",
            )
        if snapshot is not None:
            self._update_recorder_health(snapshot.recorder)
        return RuntimeHealthObservation(snapshot=snapshot, unavailable_reason=reason)

    async def _processing_record_failure_log(
        self,
        maintenance_reason: str | None,
    ) -> FailureLogObservation:
        before = self._processing_runtime_snapshot()
        reason = before.local_observation_reason(maintenance_reason)
        if reason is not None:
            return FailureLogObservation((), reason)
        async with self.module.observe_provider_root() as root_available:
            if not root_available:
                return FailureLogObservation((), "busy")
            acquired = self._processing_runtime_snapshot()
            reason = acquired.local_observation_reason(maintenance_reason)
            if acquired.generation != before.generation or reason is not None:
                return FailureLogObservation((), reason or "busy")
            entries = await run_blocking(self._store.failure_log, limit=50)
            after = self._processing_runtime_snapshot()
            reason = after.local_observation_reason(maintenance_reason)
            if after.generation != before.generation or reason is not None:
                return FailureLogObservation((), reason or "busy")
            return FailureLogObservation(entries)

    async def _processing_record_sources(
        self,
        maintenance_reason: str | None,
    ) -> ProcessingSourceObservations:
        before = self._processing_runtime_snapshot()
        reason = before.local_observation_reason(maintenance_reason)
        if reason is not None:
            unavailable = SourceObservation(
                "unavailable",
                reason=reason,
            )
            return ProcessingSourceObservations(
                everos=unavailable,
                capture=unavailable,
                calls=unavailable,
            )
        reader = self._insight_reader
        if reader is None:
            raise self._unavailable()
        observation = await run_blocking(reader.source_observation)
        after = self._processing_runtime_snapshot()
        current_reason = after.local_observation_reason(maintenance_reason)
        if after.generation != before.generation or current_reason is not None:
            unavailable = SourceObservation(
                "unavailable",
                reason=current_reason or "busy",
            )
            return ProcessingSourceObservations(
                everos=unavailable,
                capture=unavailable,
                calls=unavailable,
            )
        return observation

    async def _processing_record_provider_checks(
        self,
        maintenance_reason: str | None,
    ) -> ProviderCheckProjection:
        before = self._processing_runtime_snapshot()
        reason = before.local_observation_reason(maintenance_reason)
        if reason is not None:
            return ProviderCheckProjection(
                source=SourceObservation("unavailable", reason=reason),
                items=(),
            )
        reader = self._insight_reader
        if reader is None:
            raise self._unavailable()
        items = await run_blocking(reader.installation_preflight_calls)
        after = self._processing_runtime_snapshot()
        current_reason = after.local_observation_reason(maintenance_reason)
        if after.generation != before.generation or current_reason is not None:
            return ProviderCheckProjection(
                source=SourceObservation(
                    "unavailable",
                    reason=current_reason or "busy",
                ),
                items=(),
            )
        return ProviderCheckProjection(
            source=SourceObservation(
                "available",
                observed_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            ),
            items=items,
        )

    async def _processing_record_maintenance(
        self,
        operator_ref: str | None,
        observation: MaintenanceObservation,
    ) -> MaintenanceResult:
        return await self._require_maintenance().maintenance_payload(
            operator_ref=operator_ref,
            observation=observation,
        )

    def _update_recorder_health(
        self,
        health: dict[str, str | None],
        *,
        observed_at: str | None = None,
    ) -> None:
        self._recorder_health = dict(health)
        self._sidecar.observe_recorder_health(self._recorder_health)
        self._processing_record.observe_recorder(
            self._recorder_health,
            observed_at=observed_at,
        )

    async def processing_record_payload(
        self,
        *,
        operator_ref: str | None = None,
        verified_user_key: str | None = None,
    ) -> dict[str, Any]:
        summary = await self._processing_record.read_record(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
        )
        return _processing_record_payload(summary)

    async def status_payload(self) -> dict[str, Any]:
        runtime = await self._processing_record.read_status()
        return _runtime_health_payload(runtime)

    async def failure_log_payload(
        self,
        *,
        operator_ref: str | None = None,
        verified_user_key: str | None = None,
    ) -> dict[str, Any]:
        projection = await self._processing_record.read_failures(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
        )
        anomalies, maintenance = projection
        if anomalies.source.status == "unavailable":
            raise self._unavailable()
        payload: dict[str, Any] = {
            "status": "ok",
            "items": [asdict(entry) for entry in anomalies.items],
        }
        if maintenance.clear_in_progress is not None and maintenance.clear_in_progress.state == "failed":
            payload["clear_in_progress"] = _clear_in_progress_payload(maintenance.clear_in_progress)
        return payload

    async def maintenance_payload(
        self,
        *,
        operator_ref: str | None = None,
        verified_user_key: str | None = None,
    ) -> dict[str, Any]:
        """Return cheap local maintenance facts without probing or scanning EverOS."""

        result = await self._processing_record.read_maintenance(
            verified_user_key=verified_user_key,
            operator_ref=operator_ref,
        )
        return {
            "status": "ok",
            "data_exists": result.data_exists,
            "can_clear": result.can_clear,
            "clear_in_progress": _clear_in_progress_payload(result.clear_in_progress),
        }

    def principal_for_user_key(self, user_key: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.principal_for_user_key(user_key)

    def project_for_workdir(self, workdir: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.project_for_workdir(workdir)

    async def resolve_current_session_scope(self, raw_session_id: str) -> tuple[str, str] | None:
        """Recover a trusted capture scope from durable current-epoch state."""

        if not self.available:
            return None
        return await asyncio.to_thread(
            self._store.resolve_current_session_scope,
            raw_session_id,
        )

    async def resolve_current_session_scopes(
        self,
        raw_session_id: str,
    ) -> tuple[tuple[str, str], ...] | None:
        """Recover all trusted capture scopes for a terminal session transition."""

        if not self.available:
            return None
        return await asyncio.to_thread(
            self._store.resolve_current_session_scopes,
            raw_session_id,
        )

    async def final_flush(
        self,
        *,
        principal_id: str,
        project_id: str,
        raw_session_id: str,
        deadline_seconds: float = 5.0,
    ) -> bool:
        """Fence one trusted canonical session at a centralized lifecycle boundary."""

        if not self.available:
            return False
        return await self.module.final_flush(
            principal_id=principal_id,
            project_id=project_id,
            raw_session_id=raw_session_id,
            deadline_seconds=deadline_seconds,
        )

    async def run_session_lifecycle(
        self,
        *,
        principal_id: str,
        project_id: str,
        raw_session_id: str,
        operation: Callable[[], Awaitable[_SessionLifecycleResult]],
        deadline_seconds: float = 5.0,
    ) -> _SessionLifecycleResult:
        """Flush and run one destructive session transition under one fence."""

        if not self.available:
            return await operation()
        return await self.module.run_session_lifecycle(
            principal_id=principal_id,
            project_id=project_id,
            raw_session_id=raw_session_id,
            operation=operation,
            deadline_seconds=deadline_seconds,
        )

    async def run_session_scopes_lifecycle(
        self,
        *,
        scopes: tuple[tuple[str, str], ...],
        raw_session_id: str,
        operation: Callable[[], Awaitable[_SessionLifecycleResult]],
        deadline_seconds: float = 5.0,
    ) -> _SessionLifecycleResult:
        """Flush all session scopes and run one transition under every fence."""

        canonical_scopes = tuple(dict.fromkeys(scopes))
        if (
            not canonical_scopes
            or not isinstance(raw_session_id, str)
            or not raw_session_id
            or any(
                not is_principal_id(principal_id) or not is_project_id(project_id)
                for principal_id, project_id in canonical_scopes
            )
        ):
            raise ValueError("invalid canonical Memory session scopes")
        if not self.available:
            return await operation()
        return await self.module.run_session_scopes_lifecycle(
            scopes=canonical_scopes,
            raw_session_id=raw_session_id,
            operation=operation,
            deadline_seconds=deadline_seconds,
        )

    async def profile_payload(self, principal_id: str, project_id: str) -> dict[str, Any]:
        if not self.available:
            return {"status": "failed", "error": "memory_store_unavailable"}
        result = await self.module.profile(principal_id=principal_id, project_id=project_id)
        # Derived from this request's own result. Reading it off the shared
        # provider let a concurrent read for another principal decide what this
        # caller was told.
        empty = isinstance(result, MemoryItems) and not result.items
        return {
            **_result_payload(result),
            "profile_warning": "empty" if empty else None,
        }

    async def list_episodes_payload(
        self,
        principal_id: str,
        project_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Return one single-project processed-episode page."""

        if not self.available:
            return {"status": "failed", "error": "memory_store_unavailable"}
        result = await self.module.list_episodes(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
        )
        if isinstance(result, OperationFailed):
            return {"status": result.status, "error": result.error}
        if isinstance(result, MemoryListPage):
            return memory_list_page_payload(result)
        return {"status": "failed", "error": "memory_processing_failed"}

    async def list_all_episodes_payload(
        self,
        principal_id: str,
        *,
        cursor: str | None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Merge this principal's project pages behind an Avibe cursor."""

        if not self.available:
            return {"status": "failed", "error": "memory_store_unavailable"}
        if (
            not is_principal_id(principal_id)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MEMORY_LIST_PROVIDER_PAGE_SIZE
        ):
            return {"status": "failed", "error": "memory_invalid_input"}
        try:
            projects = await run_blocking(self.list_memory_projects, principal_id)
        except Exception:
            return {"status": "failed", "error": "memory_store_unavailable"}
        if not projects:
            projects = (DEFAULT_MEMORY_PROJECT_ID,)
        projects = tuple(sorted(projects))
        fingerprint = _memory_list_catalog_fingerprint(principal_id, projects)
        try:
            boundaries, page_hints, total_hints = _decode_memory_list_cursor(
                cursor,
                projects=projects,
                fingerprint=fingerprint,
            )
        except ValueError:
            return {"status": "failed", "error": "memory_invalid_input"}

        deadline = time.monotonic() + _MEMORY_LIST_AGGREGATE_TIMEOUT_SECONDS
        candidates: list[MemoryListItem] = []
        warnings: list[MemoryListWarningCode] = []
        totals: dict[str, int] = {}
        failures: list[OperationFailed] = []
        available_counts: dict[str, int] = {}
        project_has_more: dict[str, bool] = {}
        candidate_page_hints: dict[tuple[str, str], int] = {}
        complete = True
        async with _concurrent_episode_lists(
            self.module,
            deadline=deadline,
        ) as list_episodes:
            project_windows = await asyncio.gather(
                *(
                    self._list_project_window(
                        principal_id,
                        project_id,
                        list_episodes=list_episodes,
                        boundary=boundaries[project_id],
                        page_hint=page_hints[project_id],
                        total_hint=total_hints[project_id],
                        limit=limit,
                        deadline=deadline,
                    )
                    for project_id in projects
                )
            )
        for project_id, window in zip(projects, project_windows):
            if isinstance(window, OperationFailed):
                complete = False
                failures.append(window)
                warning: MemoryListWarningCode = (
                    "memory_list_truncated"
                    if window.error == "memory_provider_timeout"
                    else "memory_list_partial"
                )
                warnings.append(warning)
                continue
            (
                items,
                total_count,
                project_warnings,
                has_more,
                window_complete,
                item_page_hints,
            ) = window
            candidates.extend(items)
            candidate_page_hints.update(
                ((project_id, item.id), item_page_hints[item.id])
                for item in items
            )
            totals[project_id] = total_count
            available_counts[project_id] = len(items)
            project_has_more[project_id] = has_more
            warnings.extend(project_warnings)
            if not window_complete:
                complete = False

        if not totals and failures:
            failure = next(
                (
                    item
                    for item in failures
                    if item.error == "memory_provider_timeout"
                ),
                failures[0],
            )
            return {"status": failure.status, "error": failure.error}

        ordered = sorted(candidates, key=lambda item: (item.project, item.id))
        ordered.sort(
            key=lambda item: _memory_list_instant(item.timestamp),
            reverse=True,
        )
        selected = tuple(ordered[:limit])
        next_boundaries = dict(boundaries)
        next_page_hints = dict(page_hints)
        next_total_hints = dict(total_hints)
        selected_counts = {project_id: 0 for project_id in projects}
        for item in selected:
            selected_counts[item.project] += 1
            next_boundaries[item.project] = (item.timestamp, item.id)
            next_page_hints[item.project] = candidate_page_hints[(item.project, item.id)]
            next_total_hints[item.project] = totals[item.project]

        has_more = any(
            project_has_more[project_id]
            or selected_counts[project_id] < available_counts[project_id]
            for project_id in totals
        )
        if not complete:
            has_more = True
        next_cursor = (
            _encode_memory_list_cursor(
                fingerprint,
                next_boundaries,
                next_page_hints,
                next_total_hints,
            )
            if has_more
            else None
        )
        return {
            "status": "ok",
            "items": memory_list_page_payload(
                MemoryListPage(
                    items=selected,
                    page=1,
                    page_size=limit,
                    count=len(selected),
                    total_count=sum(totals.values()),
                )
            )["items"],
            "count": len(selected),
            "total_count": sum(totals.values()) if complete else None,
            "warnings": list(dict.fromkeys(warnings)),
            "next_cursor": next_cursor,
        }

    async def _list_project_window(
        self,
        principal_id: str,
        project_id: str,
        *,
        list_episodes: Callable[..., Awaitable[MemoryListResult]],
        boundary: tuple[str, str] | None,
        page_hint: int,
        total_hint: int | None,
        limit: int,
        deadline: float,
    ) -> (
        tuple[
            tuple[MemoryListItem, ...],
            int,
            tuple[MemoryListWarningCode, ...],
            bool,
            bool,
            dict[str, int],
        ]
        | OperationFailed
    ):
        page = 1
        items_by_id: dict[str, MemoryListItem] = {}
        item_page_hints: dict[str, int] = {}
        timestamp_page_hints: dict[datetime, int] = {}
        warnings: list[MemoryListWarningCode] = []
        total_count: int | None = None
        previous_page: tuple[int, MemoryListPage] | None = None
        open_timestamp: datetime | None = None
        boundary_instant = (
            _memory_list_instant(boundary[0])
            if boundary is not None
            else None
        )
        boundary_group_page_hint = page_hint

        def retry_result(error: str, observed_total: int):
            warning: MemoryListWarningCode = (
                "memory_list_truncated"
                if error == "memory_provider_timeout"
                else "memory_list_partial"
            )
            return (
                (),
                observed_total,
                tuple(dict.fromkeys((*warnings, warning))),
                True,
                False,
                {},
            )

        async def read_page(page_number: int) -> MemoryListResult:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return OperationFailed(error="memory_provider_timeout")
            try:
                return await asyncio.wait_for(
                    list_episodes(
                        principal_id=principal_id,
                        project_id=project_id,
                        page=page_number,
                        page_size=_MEMORY_LIST_PROVIDER_PAGE_SIZE,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return OperationFailed(error="memory_provider_timeout")

        def failure_result(error):
            if not items_by_id:
                return OperationFailed(error=error)
            ordered = _order_project_memory_list_items(items_by_id.values())
            safe = tuple(
                item
                for item in ordered
                if open_timestamp is not None
                and _memory_list_instant(item.timestamp) > open_timestamp
            )
            warning: MemoryListWarningCode = (
                "memory_list_truncated"
                if error == "memory_provider_timeout"
                else "memory_list_partial"
            )
            return (
                safe[:limit],
                total_count or 0,
                tuple(dict.fromkeys((*warnings, warning))),
                True,
                False,
                {
                    item.id: item_page_hints[item.id]
                    for item in safe[:limit]
                },
            )

        async def locate_boundary_page() -> tuple[int | None, int] | OperationFailed:
            first = await read_page(page_hint)
            if isinstance(first, OperationFailed):
                return first
            warnings.extend(first.warnings)
            observed_total = first.total_count
            if observed_total == 0:
                return (1, 0)
            last_page = (
                observed_total + _MEMORY_LIST_PROVIDER_PAGE_SIZE - 1
            ) // _MEMORY_LIST_PROVIDER_PAGE_SIZE
            probes = {page_hint: first}

            async def probe(page_number: int) -> MemoryListPage | OperationFailed | None:
                cached = probes.get(page_number)
                if cached is not None:
                    return cached
                result = await read_page(page_number)
                if isinstance(result, OperationFailed):
                    return result
                warnings.extend(result.warnings)
                if result.total_count != observed_total:
                    return None
                probes[page_number] = result
                return result

            pivot = min(page_hint, last_page)
            pivot_result = await probe(pivot)
            if isinstance(pivot_result, OperationFailed):
                return pivot_result
            if pivot_result is None:
                return (None, observed_total)

            def crosses_boundary(result: MemoryListPage) -> bool:
                return bool(result.items) and min(
                    _memory_list_instant(item.timestamp)
                    for item in result.items
                ) <= boundary_instant

            if crosses_boundary(pivot_result):
                low, high = 1, pivot
            else:
                low, high = pivot + 1, last_page + 1
            while low < high:
                middle = (low + high) // 2
                if middle == last_page + 1:
                    high = middle
                    continue
                middle_result = await probe(middle)
                if isinstance(middle_result, OperationFailed):
                    return middle_result
                if middle_result is None:
                    return (None, observed_total)
                if crosses_boundary(middle_result):
                    high = middle
                else:
                    low = middle + 1
            return (low, observed_total)

        if boundary_instant is not None:
            location = await locate_boundary_page()
            if isinstance(location, OperationFailed):
                return failure_result(location.error)
            boundary_page, observed_total = location
            if boundary_page is None:
                return retry_result("memory_list_partial", observed_total)
            boundary_group_page_hint = max(
                1,
                min(
                    boundary_page,
                    (
                        observed_total + _MEMORY_LIST_PROVIDER_PAGE_SIZE - 1
                    )
                    // _MEMORY_LIST_PROVIDER_PAGE_SIZE,
                ),
            )
            page = max(1, boundary_page - 1)

        while True:
            result = await read_page(page)
            if isinstance(result, OperationFailed):
                return failure_result(result.error)
            if (
                total_count is None
                and page > 1
                and (page - 1) * _MEMORY_LIST_PROVIDER_PAGE_SIZE
                >= result.total_count
            ):
                last_page = max(
                    1,
                    (
                        result.total_count
                        + _MEMORY_LIST_PROVIDER_PAGE_SIZE
                        - 1
                    )
                    // _MEMORY_LIST_PROVIDER_PAGE_SIZE,
                )
                page = max(1, last_page - 1)
                boundary_group_page_hint = min(
                    boundary_group_page_hint,
                    last_page,
                )
                warnings.extend(result.warnings)
                continue
            if total_count is not None and result.total_count != total_count:
                warnings.extend(result.warnings)
                return retry_result("memory_list_partial", result.total_count)
            if previous_page is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return retry_result(
                        "memory_provider_timeout",
                        result.total_count,
                    )
                previous_page_number, previous_page_result = previous_page
                try:
                    refreshed_previous = await asyncio.wait_for(
                        list_episodes(
                            principal_id=principal_id,
                            project_id=project_id,
                            page=previous_page_number,
                            page_size=_MEMORY_LIST_PROVIDER_PAGE_SIZE,
                        ),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    return retry_result(
                        "memory_provider_timeout",
                        result.total_count,
                    )
                if isinstance(refreshed_previous, OperationFailed):
                    return retry_result(
                        refreshed_previous.error,
                        result.total_count,
                    )
                if refreshed_previous != previous_page_result:
                    warnings.extend(refreshed_previous.warnings)
                    return retry_result("memory_list_partial", result.total_count)
            total_count = result.total_count
            warnings.extend(result.warnings)
            if boundary_instant is not None:
                timestamp_page_hints[boundary_instant] = boundary_group_page_hint
            for item in result.items:
                if item.id not in items_by_id and _memory_list_after_boundary(
                    item,
                    boundary,
                ):
                    items_by_id[item.id] = item
                    instant = _memory_list_instant(item.timestamp)
                    group_page = timestamp_page_hints.setdefault(instant, page)
                    item_page_hints[item.id] = group_page

            ordered = _order_project_memory_list_items(items_by_id.values())
            exhausted = (
                result.count < _MEMORY_LIST_PROVIDER_PAGE_SIZE
                or page * _MEMORY_LIST_PROVIDER_PAGE_SIZE >= total_count
            )
            if exhausted:
                break
            if len(ordered) > limit and result.items:
                cutoff = _memory_list_instant(ordered[limit - 1].timestamp)
                oldest_in_page = min(
                    _memory_list_instant(item.timestamp)
                    for item in result.items
                )
                if oldest_in_page < cutoff:
                    break
            if result.items:
                open_timestamp = min(
                    _memory_list_instant(item.timestamp)
                    for item in result.items
                )
            previous_page = (page, result)
            page += 1
        ordered = _order_project_memory_list_items(items_by_id.values())
        return (
            tuple(ordered[:limit]),
            total_count or 0,
            tuple(dict.fromkeys(warnings)),
            len(ordered) > limit,
            True,
            {
                item.id: item_page_hints[item.id]
                for item in ordered[:limit]
            },
        )

    def list_memory_projects(self, principal_id: str) -> tuple[str, ...]:
        if not self.available:
            return (DEFAULT_MEMORY_PROJECT_ID,)
        return self._store.list_memory_projects(principal_id)

    async def search_payload(
        self,
        query: str,
        policy: RecallPolicy,
        principal_id: str,
        project_id: str,
        *,
        current_session_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.available:
            return {"status": "failed", "error": "memory_store_unavailable"}
        if project_id == MEMORY_SEARCH_ALL_PROJECTS:
            return _result_payload(
                await self._recall_all_projects(
                    query,
                    policy=policy,
                    principal_id=principal_id,
                )
            )
        result = await self.module.recall(
            query,
            policy=policy,
            principal_id=principal_id,
            project_id=project_id,
            current_session_id=current_session_id,
        )
        if isinstance(result, RecallItems):
            result = replace(
                result,
                items=tuple(
                    replace(item, project=project_id) for item in result.items
                ),
            )
        return _result_payload(result)

    async def _recall_all_projects(
        self,
        query: str,
        *,
        policy: RecallPolicy,
        principal_id: str,
    ) -> RecallResult:
        if policy.mode == "agentic" or policy.include_current_session:
            return OperationFailed(error="memory_invalid_input")
        deadline = time.monotonic() + 20.0
        projects = await run_blocking(self.list_memory_projects, principal_id)
        if not projects:
            projects = (DEFAULT_MEMORY_PROJECT_ID,)
        collected: list[MemoryItem] = []
        warnings: list[MemoryWarningCode] = []
        first_failure: OperationFailed | None = None
        succeeded = False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return RecallItems(
                items=(),
                requested_mode=policy.mode,
                effective_mode="keyword",
                warnings=("memory_search_truncated",),
            )
        try:
            effective_mode = await asyncio.wait_for(
                self.module.resolve_recall_mode(policy),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return RecallItems(
                items=(),
                requested_mode=policy.mode,
                effective_mode="keyword",
                warnings=("memory_search_truncated",),
            )
        if isinstance(effective_mode, OperationFailed):
            return effective_mode
        for index, project_id in enumerate(projects):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                warnings.append("memory_search_truncated")
                break
            scope_policy = replace(
                policy,
                include_current_session=False,
                include_profile=bool(policy.include_profile and index == 0),
            )
            try:
                result = await asyncio.wait_for(
                    self.module.recall(
                        query,
                        policy=scope_policy,
                        principal_id=principal_id,
                        project_id=project_id,
                        effective_mode=effective_mode,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                warnings.append("memory_search_truncated")
                break
            if isinstance(result, OperationFailed):
                if first_failure is None:
                    first_failure = result
                warnings.append("memory_search_partial")
                continue
            succeeded = True
            effective_mode = result.effective_mode
            collected.extend(
                replace(item, project=project_id) for item in result.items
            )
        if not succeeded:
            return first_failure or RecallItems(
                items=(),
                requested_mode=policy.mode,
                effective_mode=effective_mode,
                warnings=tuple(dict.fromkeys(warnings)),
            )
        merged = _merge_search_items(collected, limit=policy.max_results)
        unique_warnings = tuple(dict.fromkeys(warnings))
        return RecallItems(
            items=merged,
            requested_mode=policy.mode,
            effective_mode=effective_mode,
            warnings=unique_warnings,
        )

    async def log_entries_payload(
        self,
        principal_id: str,
        project_id: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        reader = self._insight_reader
        if not self.available or reader is None:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return await self._run_insight_read(
            lambda: reader.list_entries((principal_id, project_id), cursor, limit)
        )

    async def log_entry_payload(
        self,
        principal_id: str,
        project_id: str,
        memcell_id: str,
    ) -> dict[str, Any]:
        reader = self._insight_reader
        if not self.available or reader is None:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return await self._run_insight_read(
            lambda: reader.entry_detail((principal_id, project_id), memcell_id)
        )

    async def admin_log_entries_payload(
        self,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        reader = self._insight_reader
        if not self.available or reader is None:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return await self._run_insight_read(
            lambda: reader.list_admin_entries(cursor, limit)
        )

    async def admin_log_entry_payload(self, memcell_id: str) -> dict[str, Any]:
        reader = self._insight_reader
        if not self.available or reader is None:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return await self._run_insight_read(
            lambda: reader.admin_entry_detail(memcell_id)
        )

    async def _run_insight_read(
        self,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self.module.lifecycle():
            return await run_blocking(operation)

    async def clear(self, *, operator_ref: str) -> dict[str, Any]:
        if self._retired or getattr(self._restart_config, "recovery_intent", None) == "factory_reset":
            return {"status": "failed", "error": "memory_operation_in_progress"}
        if not self.available:
            raise self._unavailable()
        maintenance = self._require_maintenance()
        return await self._run_clear_with_operation_lease(
            lambda: maintenance.clear(operator_ref=operator_ref)
        )

    async def _run_clear_with_operation_lease(
        self,
        operation: Callable[[], Awaitable[ClearResult]],
    ) -> dict[str, Any]:
        if self._retired or getattr(self._restart_config, "recovery_intent", None) == "factory_reset":
            return {"status": "failed", "error": "memory_operation_in_progress"}
        if self._rebuild_running() or self._repair_running():
            return {"status": "failed", "error": "memory_operation_in_progress"}
        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
            return _clear_result_payload(await operation())
        except MemoryOperationBusy:
            return {"status": "failed", "error": "memory_operation_in_progress"}
        finally:
            await run_blocking(lease.release)

    async def _pause_clear_claims(self) -> None:
        if not await self.module.quiesce_claims_for_clear():
            raise RuntimeError("Memory worker did not quiesce before clear")

    async def _quiesce_for_clear(self, claims_already_paused: bool = False) -> None:
        if not claims_already_paused:
            await self._pause_clear_claims()
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False
        await self._stop_call_log_retention()
        self._set_recorder_health_disabled()

    async def _delete_clear_surface(
        self,
        surface: ClearSurface,
        target_epoch: int,
    ) -> None:
        if surface.surface == "queue":
            await run_blocking(
                self._store.reset_for_clear,
                target_epoch=target_epoch,
                release_clear_fence=False,
            )
            return
        if surface.surface == "provider":

            def reset_provider_root() -> None:
                meta = self._store.ensure_meta()
                if self._provider_root.exists():
                    self._provider_root_owner.recreate_empty(
                        meta,
                        self._active_provider_root_metadata(),
                    )
                else:
                    self._provider_root_owner.ensure(
                        meta,
                        self._active_provider_root_metadata(),
                    )

            await run_blocking(reset_provider_root)
            return
        if surface.surface == "call_log":
            await run_blocking(clear_call_log, self._call_log_db_path)
            self._sidecar.reset_host_retention_after_clear()
            self._set_recorder_health_disabled()
            return
        if surface.surface == "attachments":
            await self.module.clear_attachments()
            return
        raise RuntimeError("unknown Memory clear surface")

    async def _resume_after_clear(self) -> None:
        """Keep lifecycle ownership until runtime reconciliation settles."""

        task = asyncio.create_task(self._resume_after_clear_once())
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except Exception:
                break
        if cancellation is not None:
            try:
                task.result()
            except (Exception, asyncio.CancelledError):
                pass
            raise cancellation
        task.result()

    async def _resume_after_clear_once(self) -> None:
        if not self._config.enabled:
            self.module.resume_claims()
            return
        try:
            reconciled = await self._reconcile_locked(
                self._config,
                claims_already_paused=True,
            )
        except Exception:
            reconciled = {"ok": False}
        if reconciled.get("ok") is True:
            self._restart_config = deepcopy(self._config)
        else:
            self._runtime_error = "memory_sidecar_unavailable"

    def _require_maintenance(self) -> MemoryMaintenance:
        maintenance = self._maintenance
        if maintenance is None:
            raise self._unavailable()
        return maintenance
    async def install_artifact(self) -> dict[str, Any]:
        """Install or repair EverOS through this controller-owned lifecycle."""
        return await self._install_artifact()

    async def _install_artifact(self) -> dict[str, Any]:
        """Run one artifact install while retaining its blocking ensure call."""

        if not self.available:
            return {"ok": False, "reason": "memory_store_unavailable", "download_error": None}
        if self._rebuild_running():
            return {
                "ok": False,
                "reason": "memory_operation_in_progress",
                "download_error": None,
            }

        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
        except MemoryOperationBusy:
            return {
                "ok": False,
                "reason": "memory_operation_in_progress",
                "download_error": None,
            }
        try:
            return await self._install_artifact_with_lease()
        finally:
            await run_blocking(lease.release)

    async def _install_artifact_with_lease(self) -> dict[str, Any]:
        """Run the installer after destructive-operation admission succeeds."""

        self._activation_loop = asyncio.get_running_loop()
        async with self._reconcile_lock:
            if self._exclusive_operation_running():
                return {
                    "ok": False,
                    "reason": "memory_operation_in_progress",
                    "download_error": None,
                }
            if self._artifact_installing:
                return {
                    "ok": False,
                    "reason": "memory_runtime_install_requires_disabled_memory",
                    "download_error": None,
                }
            # Shared ensure(force=True) can delete the active fingerprint directory
            # before invoking our activation bridge. Stop a retained supervisor,
            # including its terminal "down" state, while claims are fenced so a
            # repair can safely replace the executable it might otherwise relaunch.
            supervisor = self._process
            # A HEALTHY running sidecar must not be force-stopped/replaced through
            # Repair — that requires a coordinated disable first. Only a retained
            # supervisor in its terminal "down" state (no live child) may be stopped
            # here so Repair can recover enabled/down Memory.
            if supervisor is not None and supervisor.running:
                return {
                    "ok": False,
                    "reason": "memory_runtime_install_requires_disabled_memory",
                    "download_error": None,
                }
            if supervisor is not None:
                async with self.module.lifecycle():
                    try:
                        claims_paused = await self.module.quiesce_claims()
                    except Exception:
                        claims_paused = False
                    if not claims_paused:
                        self._runtime_error = "memory_runtime_install_failed"
                        return {
                            "ok": False,
                            "reason": self._runtime_error,
                            "download_error": None,
                        }
                    try:
                        await supervisor.stop()
                    except Exception:
                        self._runtime_error = "memory_runtime_install_failed"
                        return {
                            "ok": False,
                            "reason": self._runtime_error,
                            "download_error": None,
                        }
                    self._process = None
                    self._process_records_calls = False
                    self._ensure_call_log_retention()
            self._artifact_installing = True
        ensure_task = asyncio.create_task(
            asyncio.to_thread(self._artifact_manager.ensure, force=True)
        )
        cancellation: asyncio.CancelledError | None = None
        ensure_failed = False
        payload: object = None
        try:
            while not ensure_task.done():
                try:
                    await asyncio.shield(ensure_task)
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
                except Exception:
                    # Interpret the settled task below so installer failures
                    # stay inside the public result contract.
                    pass
            if cancellation is not None:
                try:
                    ensure_task.result()
                except (Exception, asyncio.CancelledError):
                    pass
            else:
                try:
                    payload = ensure_task.result()
                except Exception:
                    ensure_failed = True
        finally:
            while self._artifact_installing:
                try:
                    async with self._reconcile_lock:
                        self._artifact_installing = False
                except asyncio.CancelledError as error:
                    cancellation = cancellation or error
            if self._maintenance is not None:
                self._maintenance.ensure_housekeeping()
        if cancellation is not None:
            raise cancellation
        if ensure_failed:
            return {
                "ok": False,
                "reason": "memory_runtime_install_failed",
                "download_error": None,
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "reason": "memory_runtime_install_failed",
                "download_error": None,
            }
        reason = payload.get("reason")
        download_error = payload.get("download_error")
        return {
            "ok": bool(payload.get("ok")),
            "reason": reason if isinstance(reason, str) else None,
            "download_error": download_error if isinstance(download_error, dict) else None,
        }

    async def restart(self) -> dict[str, Any]:
        """Join or start one process-only replacement of the Memory sidecar."""

        if self._retired:
            return {"ok": False, "error": "memory_operation_in_progress"}
        if self._rebuild_running() or self._repair_running():
            return {"ok": False, "error": "memory_operation_in_progress"}
        task = self._restart_task
        if task is None or task.done():
            task = asyncio.create_task(self._restart_once(), name="memory-restart")
            self._restart_task = task

            def clear_restart(completed: asyncio.Task[dict[str, Any]]) -> None:
                if self._restart_task is completed:
                    self._restart_task = None

            task.add_done_callback(clear_restart)
        return await asyncio.shield(task)

    async def preflight(self, config: MemoryConfig | None = None) -> dict[str, Any]:
        candidate = deepcopy(config or self._config)
        provider = EverOSPort(
            self._socket_path,
            llm_base_url=candidate.processing.llm.base_url,
            llm_model=candidate.processing.llm.model,
            llm_api_key=candidate.processing.llm.api_key,
            embedding_base_url=candidate.processing.embedding.base_url,
            embedding_model=candidate.processing.embedding.model,
            embedding_api_key=candidate.processing.embedding.api_key,
            rerank_base_url=(candidate.processing.rerank.base_url if candidate.processing.rerank else None),
            rerank_model=(candidate.processing.rerank.model if candidate.processing.rerank else None),
            rerank_api_key=(candidate.processing.rerank.api_key if candidate.processing.rerank else None),
            preflight_call_recorder=self._record_preflight_call,
        )
        return (await provider.preflight()).payload()

    def _record_preflight_call(
        self,
        *,
        side,
        model=None,
        request,
        response,
        failure,
        base_url=None,
        api_key=None,
        started_at_ms,
        duration_ms,
    ) -> None:
        record_preflight_call(
            self._call_log_db_path,
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
            kind=side,
            model=model,
            request=request,
            response=response,
            status="error" if failure is not None else "ok",
            error=failure.diagnostic.message if failure is not None else None,
            provider_base_urls=(base_url,) if base_url else (),
            exact_redaction_values=(api_key,) if api_key else (),
        )

    async def rebuild(self) -> dict[str, Any]:
        """Join or start one retained embedding-index rebuild over the cascade child."""

        if self._closing or self._retired or self._repair_running():
            return {
                "ok": False,
                "error": "memory_operation_in_progress",
                "result": "failed",
            }
        task = self._rebuild_task
        if task is None or task.done():
            task = asyncio.create_task(self._rebuild_once(), name="memory-rebuild")
            self._rebuild_task = task

            def clear_rebuild(completed: asyncio.Task[dict[str, Any]]) -> None:
                if self._rebuild_task is completed:
                    self._rebuild_task = None

            task.add_done_callback(clear_rebuild)
        return await asyncio.shield(task)

    def _rebuild_running(self) -> bool:
        task = self._rebuild_task
        return task is not None and not task.done()

    def _repair_running(self) -> bool:
        task = self._repair_task
        return task is not None and not task.done()

    def _restart_running(self) -> bool:
        task = self._restart_task
        return task is not None and not task.done()

    def _exclusive_operation_running(self) -> bool:
        current = asyncio.current_task()
        return bool(
            (self._rebuild_running() and current is not self._rebuild_task)
            or (self._repair_running() and current is not self._repair_task)
        )

    async def repair(self) -> dict[str, Any]:
        """Join or start one live pathless cascade sync and return final health."""

        if (
            self._closing
            or self.factory_reset_pending
            or self.rebuild_pending
            or self._rebuild_running()
            or self._restart_running()
            or self._reconcile_lock.locked()
        ):
            return {
                "ok": False,
                "error": "memory_operation_in_progress",
                "result": "failed",
            }
        task = self._repair_task
        if task is None or task.done():
            task = asyncio.create_task(self._repair_once(), name="memory-repair")
            self._repair_task = task

            def clear_repair(completed: asyncio.Task[dict[str, Any]]) -> None:
                if self._repair_task is completed:
                    self._repair_task = None

            task.add_done_callback(clear_repair)
        return await asyncio.shield(task)

    async def _repair_once(self) -> dict[str, Any]:
        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
            if (
                self._closing
                or self.factory_reset_pending
                or self.rebuild_pending
                or self._rebuild_running()
                or self._restart_running()
                or self._reconcile_lock.locked()
                or self._artifact_installing
                or self._maintenance_open()
            ):
                return {
                    "ok": False,
                    "error": "memory_operation_in_progress",
                    "result": "failed",
                }
            python = await asyncio.to_thread(self._artifact_manager.resolve_python)
            if python is None:
                return {
                    "ok": False,
                    "error": "memory_runtime_unsupported",
                    "result": "failed",
                }
            if not await asyncio.to_thread(self._artifact_manager.sync_capability):
                return {
                    "ok": False,
                    "error": "memory_runtime_unsupported",
                    "result": "failed",
                }
            admitted_python = await asyncio.to_thread(
                self._artifact_manager.resolve_python
            )
            if admitted_python != python:
                return {
                    "ok": False,
                    "error": "memory_runtime_unsupported",
                    "result": "failed",
                }
            if not self.available or self._store is None or self._module is None:
                return {
                    "ok": False,
                    "error": "memory_store_unavailable",
                    "result": "failed",
                }
            if not self._config.enabled:
                return {
                    "ok": False,
                    "error": "memory_disabled",
                    "result": "failed",
                }
            process = self._process
            if process is None or not process.running:
                return {
                    "ok": False,
                    "error": "memory_sidecar_unavailable",
                    "result": "failed",
                }
            child = EverOSSyncProcess(
                python,
                effective_home=self._effective_home,
                provider_root=self._provider_root,
                settings=_process_settings(
                    self._config,
                    call_log_db_path=self._call_log_db_path,
                ),
            )
            child_result = await child.run()
            if child_result is not SyncProcessResult.COMPLETED:
                return _repair_process_result(child_result)
            snapshot = await self._provider.health_snapshot()
            cascade = snapshot.cascade
            if cascade is None:
                return {
                    "ok": False,
                    "error": "memory_repair_failed",
                    "result": "failed",
                }
            health = dict(cascade)
            return {
                "ok": True,
                "result": (
                    "completed"
                    if health.get("healthy") is True
                    else "completed_with_warnings"
                ),
                "health": health,
            }
        except MemoryOperationBusy:
            return {
                "ok": False,
                "error": "memory_operation_in_progress",
                "result": "failed",
            }
        except Exception:
            logger.exception("Memory index repair failed")
            return {
                "ok": False,
                "error": "memory_repair_failed",
                "result": "failed",
            }
        finally:
            await run_blocking(lease.release)

    async def _restart_once(self) -> dict[str, Any]:
        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
            async with self._reconcile_lock:
                return await self._restart_locked()
        except MemoryOperationBusy:
            return {"ok": False, "error": "memory_operation_in_progress"}
        except Exception:
            logger.exception("Memory sidecar restart failed")
            return {"ok": False, "error": "memory_restart_failed"}
        finally:
            try:
                if self._maintenance is not None:
                    self._maintenance.ensure_housekeeping()
            finally:
                await run_blocking(lease.release)

    async def _rebuild_once(self) -> dict[str, Any]:
        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
            async with self._reconcile_lock:
                return await self._rebuild_locked()
        except MemoryOperationBusy:
            return {
                "ok": False,
                "error": "memory_operation_in_progress",
                "result": "failed",
            }
        except Exception:
            logger.exception("Memory embedding rebuild failed")
            self._runtime_error = "memory_rebuild_failed"
            return {
                "ok": False,
                "error": "memory_rebuild_failed",
                "result": "failed",
            }
        finally:
            try:
                if self._maintenance is not None:
                    self._maintenance.ensure_housekeeping()
            finally:
                await run_blocking(lease.release)

    async def _restart_locked(self) -> dict[str, Any]:
        """Replace the sidecar while ``_reconcile_lock`` is held."""

        self._activation_loop = asyncio.get_running_loop()
        if not self.available or self._store is None or self._module is None:
            logger.warning("Memory rebuild failed branch=store_unavailable")
            return {"ok": False, "error": "memory_store_unavailable"}
        if self._artifact_installing:
            logger.warning("Memory rebuild failed branch=artifact_installing")
            return {"ok": False, "error": "memory_restart_failed"}
        if self._rebuild_running() and asyncio.current_task() is not self._rebuild_task:
            return {"ok": False, "error": "memory_operation_in_progress"}

        replay = deepcopy(self._restart_config)
        if not replay.enabled:
            return {"ok": False, "error": "memory_disabled"}
        if replay.recovery_intent in MEMORY_RECOVERY_INTENTS:
            if replay.recovery_intent == "factory_reset":
                return {"ok": False, "error": "memory_operation_in_progress"}
            return {"ok": False, "error": "memory_embedding_rebuild_required"}

        python = await asyncio.to_thread(self._artifact_manager.resolve_python)
        if python is None:
            return {"ok": False, "error": "memory_restart_failed"}

        async with self.module.lifecycle():
            if self._maintenance_open():
                self.module.pause_claims()
                return {"ok": False, "error": "memory_clear_failed"}

            # Recovery may resume claims. Reinstate the fence synchronously,
            # before a drain task can run another claim.
            self.module.pause_claims()
            old_process = self._process
            try:
                # The grace budget applies only to the current drain tick. A
                # timeout leaves the current process/provider ownership intact.
                if not await self.module.quiesce_claims(timeout_seconds=5.0):
                    if old_process is not None and old_process.running:
                        self.module.resume_claims()
                        self._ensure_worker()
                    return {"ok": False, "error": "memory_restart_failed"}
                await self._stop_worker()
            except Exception:
                if old_process is not None and old_process.running:
                    self.module.resume_claims()
                    self._ensure_worker()
                return {"ok": False, "error": "memory_restart_failed"}

            if old_process is not None:
                try:
                    await old_process.stop()
                except Exception:
                    # Retain the supervisor: only its successful stop proves
                    # that no owned child tree remains.
                    self._runtime_error = "memory_restart_failed"
                    return {"ok": False, "error": self._runtime_error}
                self._process = None
                self._process_records_calls = False
                self._ensure_call_log_retention()

            # From this point old ownership is gone, so every failure remains
            # fail closed with claims paused.
            self._config = deepcopy(replay)
            self._configure_insight_reader(self._config)
            self._provider = EverOSPort(
                self._socket_path,
                processing_health_check=self._processing_healthy,
            )
            self.module.replace_provider(self._provider)
            try:
                meta = await asyncio.to_thread(self._store.ensure_meta)
                await run_blocking(
                    self._provider_root_owner.ensure,
                    meta,
                    self._active_provider_root_metadata(),
                )
            except Exception:
                self._runtime_error = "memory_clear_failed"
                return {"ok": False, "error": self._runtime_error}

            self.module.begin_activation(new_lease=True)
            try:
                started = await self._sidecar.start(
                    python,
                    _process_settings(
                        self._config,
                        call_log_db_path=self._call_log_db_path,
                    ),
                )
            except Exception:
                self._runtime_error = "memory_restart_failed"
                return {"ok": False, "error": self._runtime_error}
            if not started:
                self._runtime_error = "memory_sidecar_unavailable"
                return {"ok": False, "error": self._runtime_error}

            self._runtime_error = None
            self.module.resume_claims()
            self._ensure_worker()
            return {"ok": True, "state": "ready"}

    async def _rebuild_locked(self) -> dict[str, Any]:
        """Rebuild the vector index while ``_reconcile_lock`` is held."""

        self._activation_loop = asyncio.get_running_loop()
        if not self.available or self._store is None or self._module is None:
            return {
                "ok": False,
                "error": "memory_store_unavailable",
                "result": "failed",
            }
        if self._artifact_installing:
            return {
                "ok": False,
                "error": "memory_rebuild_failed",
                "result": "failed",
            }

        # The durable candidate is the only rebuild authority. Never fall back
        # to a process snapshot after a config read failure: it may be stale.
        try:
            candidate = await asyncio.to_thread(lambda: V2Config.load().memory)
        except Exception:
            logger.exception("Memory rebuild could not load the durable candidate")
            self.module.pause_claims()
            self._config = replace(self._config, recovery_intent="rebuild")
            self._restart_config = replace(
                self._restart_config,
                recovery_intent="rebuild",
            )
            self._runtime_error = "memory_rebuild_failed"
            return {
                "ok": False,
                "error": self._runtime_error,
                "result": "failed",
            }
        if candidate.recovery_intent != "rebuild":
            return {
                "ok": False,
                "error": "memory_invalid_input",
                "result": "failed",
            }

        # Publish the durable fence before any further await. Every preflight
        # failure must leave restart fenced against the same candidate.
        self._config = deepcopy(candidate)
        self._restart_config = deepcopy(candidate)
        if candidate.enabled or _memory_processing_complete(candidate):
            preflight = await self.preflight(candidate)
        else:
            # A disabled, incomplete candidate can be persisted while the
            # operator fills in its providers. An empty root needs no network
            # admission check; data-bearing roots are rejected below.
            preflight = {"ok": True}
        if preflight.get("ok") is not True:
            # Keep the durable candidate and recovery fence visible to every
            # later restart, while the retained sidecar and its reader keep
            # using the active settings until Retry succeeds.
            self._config = deepcopy(candidate)
            self._restart_config = deepcopy(candidate)
            self.module.pause_claims()
            return {**preflight, "result": "failed"}
        self.module.pause_claims()
        rebuild_settings = _process_settings(
            candidate,
            call_log_db_path=self._call_log_db_path,
        )

        async with self.module.lifecycle():
            if self._maintenance_open():
                self.module.pause_claims()
                return {
                    "ok": False,
                    "error": "memory_operation_in_progress",
                    "result": "failed",
                }

            try:
                quiesced = await self.module.quiesce_claims()
            except Exception:
                quiesced = False
            if not quiesced:
                logger.warning("Memory rebuild failed branch=quiesce")
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": "memory_rebuild_failed",
                    "result": "failed",
                }

            # Artifact and first strict inspection are admission checks. They
            # run under the claim fence while the old sidecar is still owned.
            python = await asyncio.to_thread(self._artifact_manager.resolve_python)
            if python is None:
                logger.warning("Memory rebuild failed branch=artifact_resolution")
                error = _runtime_error_for_status(
                    await asyncio.to_thread(self._artifact_manager.status)
                )
                self._runtime_error = error
                return {"ok": False, "error": error, "result": "failed"}
            try:
                pre_stop_has_data = await asyncio.to_thread(
                    self._provider_data_exists_strict
                )
            except Exception:
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": "memory_rebuild_failed",
                    "result": "failed",
                }

            if pre_stop_has_data:
                if not _rebuild_settings_usable(rebuild_settings):
                    self._runtime_error = "memory_rebuild_failed"
                    return {
                        "ok": False,
                        "error": "memory_rebuild_failed",
                        "result": "failed",
                    }

            if _memory_processing_complete(candidate):
                try:
                    candidate_healthy = await self._probe_processing(python, candidate)
                except Exception:
                    candidate_healthy = False
            else:
                candidate_healthy = True
            if not candidate_healthy:
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": "failed",
                }

            await self._stop_worker()
            await self._sidecar.stop()

            # Only an inspection after proven sidecar death decides whether a
            # rebuild child is required. The first read was admission evidence,
            # not the authoritative empty/non-empty decision.
            try:
                has_data = await asyncio.to_thread(
                    self._provider_data_exists_strict
                )
            except Exception:
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": "failed",
                }
            if has_data and not _rebuild_settings_usable(rebuild_settings):
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": "failed",
                }

            self._provider = EverOSPort(
                self._socket_path,
                processing_health_check=self._processing_healthy,
            )
            self.module.replace_provider(self._provider)

            if has_data:
                rebuild_process = EverOSRebuildProcess(
                    python,
                    effective_home=self._effective_home,
                    provider_root=self._provider_root,
                    settings=rebuild_settings,
                )
                child_result = await rebuild_process.run()
                mapped = _rebuild_public_result(child_result)
                if mapped["result"] not in {"completed", "completed_empty"}:
                    self._runtime_error = mapped["error"]
                    return mapped
            else:
                mapped = {
                    "ok": True,
                    "result": "completed_empty",
                }
                # Empty rebuilds still own a provider-root format transition.
                # Complete and verify it before clearing the durable retry fence.
                try:
                    async with self.module.provider_root_lifecycle():
                        meta = await asyncio.to_thread(self._store.ensure_meta)
                        active_metadata = self._active_provider_root_metadata()
                        root_rollback = await run_blocking(
                            self._provider_root_owner.activate_empty_format,
                            meta,
                            active_metadata,
                        )
                        try:
                            await run_blocking(
                                self._provider_root_owner.ensure,
                                meta,
                                active_metadata,
                            )
                        except Exception:
                            if root_rollback is not None:
                                await run_blocking(root_rollback.rollback)
                            raise
                except Exception:
                    self._runtime_error = "memory_rebuild_failed"
                    return {
                        "ok": False,
                        "error": self._runtime_error,
                        "result": "failed",
                    }

            settled = await run_blocking(
                self._settle_rebuild_intent,
                candidate,
            )
            if settled is None:
                self._runtime_error = "memory_rebuild_failed"
                return {
                    "ok": False,
                    "error": "memory_rebuild_failed",
                    "result": "failed",
                }

            # Settlement returns the latest durable candidate so a concurrent
            # credential correction cannot be discarded by the rebuild snapshot.
            self._config = deepcopy(settled)
            self._restart_config = deepcopy(settled)
            self._configure_insight_reader(self._config)

            try:
                if self._on_config_settled is not None:
                    self._on_config_settled(deepcopy(settled))
            except Exception:
                logger.exception("Memory rebuild could not publish the settled config")
                self._runtime_error = "memory_restart_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": mapped["result"],
                }

            if not settled.enabled:
                try:
                    disabled = await self._disable_locked(settled)
                except Exception:
                    self._runtime_error = "memory_restart_failed"
                    return {
                        "ok": False,
                        "error": self._runtime_error,
                        "result": mapped["result"],
                    }
                return {**disabled, "result": mapped["result"]}

            try:
                meta = await asyncio.to_thread(self._store.ensure_meta)
                await run_blocking(
                    self._provider_root_owner.ensure,
                    meta,
                    self._active_provider_root_metadata(),
                )
            except Exception:
                self._runtime_error = "memory_restart_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": mapped["result"],
                }

            self.module.begin_activation(new_lease=True)
            try:
                # Destructive cutover: skip ordinary healthy-replacement endpoint
                # preflight; the rebuild child already exercised the candidate.
                started = await self._sidecar.start(
                    python,
                    _process_settings(
                        self._config,
                        call_log_db_path=self._call_log_db_path,
                    ),
                )
            except Exception:
                self._runtime_error = "memory_restart_failed"
                return {
                    "ok": False,
                    "error": self._runtime_error,
                    "result": mapped["result"],
                }
            if not started:
                self._runtime_error = "memory_sidecar_unavailable"
                return {
                    "ok": False,
                    "error": "memory_sidecar_unavailable",
                    "result": mapped["result"],
                }

            self._runtime_error = None
            self.module.resume_claims()
            self._ensure_worker()
            return {
                "ok": True,
                "result": mapped["result"],
                "state": "ready",
            }

    async def close(self) -> None:
        self._closing = True
        self._sidecar.close_ready_admission()
        self._activation_loop = None
        self._advance_processing_lifecycle()

        cancellation: asyncio.CancelledError | None = None
        repair = self._repair_task
        if repair is not None and repair is not asyncio.current_task():
            if not repair.done():
                repair.cancel()
            cancellation = await self._join_shutdown_task(repair)
            try:
                repair.result()
            except (Exception, asyncio.CancelledError):
                pass

        rebuild = self._rebuild_task
        if rebuild is not None and rebuild is not asyncio.current_task():
            if not rebuild.done():
                rebuild.cancel()
            cancellation = await self._join_shutdown_task(rebuild)
            try:
                rebuild.result()
            except (Exception, asyncio.CancelledError):
                pass

        cleanup = asyncio.create_task(
            self._close_after_rebuild(),
            name="memory-runtime-close",
        )
        cleanup_cancellation = await self._join_shutdown_task(cleanup)
        cancellation = cancellation or cleanup_cancellation
        cleanup_error: BaseException | None = None
        try:
            cleanup.result()
        except BaseException as error:
            cleanup_error = error
        if cancellation is not None:
            if cleanup_error is not None:
                logger.error(
                    "Memory runtime cleanup failed while close was cancelled: %s",
                    cleanup_error,
                )
            raise cancellation
        if cleanup_error is not None:
            raise cleanup_error

    @staticmethod
    async def _join_shutdown_task(
        task: asyncio.Task[Any],
    ) -> asyncio.CancelledError | None:
        """Join one shutdown-owned task without abandoning it on cancellation."""

        cancellation: asyncio.CancelledError | None = None
        current = asyncio.current_task()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                if current is not None and current.cancelling():
                    cancellation = cancellation or error
            except BaseException:
                break
        return cancellation

    async def _close_after_rebuild(self) -> None:
        """Finish ordinary shutdown after rebuild ownership is fully released."""

        cleanup_error: BaseException | None = None
        if self._maintenance is not None:
            try:
                await self._maintenance.close()
            except BaseException as error:
                cleanup_error = error
        retained = self._restart_task
        if retained is not None and retained is not asyncio.current_task():
            try:
                await asyncio.shield(retained)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        for task in (self._artifact_activation_task, self._ready_activation_task):
            if task is None or task is asyncio.current_task():
                continue
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if self._module is not None:
            self._module.pause_claims()
            try:
                await self._stop_worker()
            except BaseException as error:
                cleanup_error = error
            async with self._module.provider_root_lifecycle():
                try:
                    if (
                        self._retired
                        and not await self._module.quiesce_claims_for_clear()
                    ):
                        raise RuntimeError(
                            "Memory worker did not quiesce during retired close"
                        )
                    await self._module.prepare_shutdown()
                except BaseException as error:
                    cleanup_error = cleanup_error or error
        try:
            await self._stop_call_log_retention()
        except BaseException as error:
            cleanup_error = cleanup_error or error
        try:
            await self._sidecar.close()
        except BaseException as error:
            cleanup_error = cleanup_error or error
        self._artifact_manager.set_activation_coordinator(None)
        if cleanup_error is not None:
            raise cleanup_error
        self._closed = True
        self._advance_processing_lifecycle()

    def _active_provider_root_metadata(self) -> ProviderRootMetadata:
        provider_root_format = (
            self._artifact_manager.provider_root_format()
            or f"everos-{EVEROS_VERSION}"
        )
        artifact_fingerprint = self._artifact_manager.artifact_fingerprint()
        return ProviderRootMetadata(
            provider_root_format=provider_root_format,
            artifact_fingerprint=artifact_fingerprint or "memory-runtime-unavailable",
            compatible_provider_root_formats=frozenset(
                {
                    provider_root_format,
                    *_active_compatible_root_formats(self._artifact_manager),
                }
            ),
        )

    def _coordinate_artifact_activation(
        self,
        candidate: MemoryArtifactCandidate,
        root_state: MemoryProviderRootState | None,
        commit: Callable[[], None],
        rollback: Callable[[], None],
    ) -> None:
        """Bridge the synchronous shared installer into the controller loop."""

        # Factory reset deletes retained roots, so its durable fence may admit
        # the pointer without inspecting them. Rebuild preserves retained data
        # and therefore requires ProviderRoot.inspect() to have established
        # compatibility before pointer-only admission.
        durable_intent = (
            self._durable_recovery_intent() if self.recovery_pending else None
        )
        if durable_intent == "factory_reset":
            commit()
            return

        if root_state is None:
            raise MemoryRuntimeActivationError("memory provider root could not be inspected")

        if durable_intent == "rebuild":
            commit()
            return

        loop = self._activation_loop
        if loop is None or loop.is_closed():
            raise MemoryRuntimeActivationError("memory controller lifecycle is unavailable")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            raise MemoryRuntimeActivationError("memory runtime activation must not block the controller loop")
        task_ready: ThreadFuture[asyncio.Task[None]] = ThreadFuture()
        task_result: ThreadFuture[None] = ThreadFuture()

        def settle_task(task: asyncio.Task[None]) -> None:
            if self._artifact_activation_task is task:
                self._artifact_activation_task = None
            if task.cancelled():
                task_result.cancel()
                return
            error = task.exception()
            if error is not None:
                task_result.set_exception(error)
            else:
                task_result.set_result(None)

        def submit_task() -> None:
            try:
                task = loop.create_task(
                    self._activate_artifact_candidate(candidate, root_state, commit, rollback)
                )
            except BaseException as exc:
                task_ready.set_exception(exc)
                return
            self._artifact_activation_task = task
            task.add_done_callback(settle_task)
            task_ready.set_result(task)

        try:
            loop.call_soon_threadsafe(submit_task)
            task = task_ready.result()
        except Exception as exc:
            raise MemoryRuntimeActivationError("memory controller lifecycle is unavailable") from exc
        try:
            task_result.result(timeout=ARTIFACT_ACTIVATION_TIMEOUT_SECONDS)
        except FutureTimeoutError as timeout_error:
            # Task.cancel() only requests cancellation. Wait on the result
            # bridge, which settles from the actual Task done callback after
            # activation rollback and reconciliation cleanup have completed.
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError as exc:
                raise MemoryRuntimeActivationError("memory controller lifecycle is unavailable") from exc
            try:
                task_result.result()
            except FutureCancelledError:
                raise MemoryRuntimeActivationError("memory runtime activation timed out") from timeout_error
            except MemoryRuntimeActivationError:
                raise
            except Exception as exc:
                raise MemoryRuntimeActivationError("memory runtime activation failed") from exc
            return
        except MemoryRuntimeActivationError:
            raise
        except Exception as exc:
            raise MemoryRuntimeActivationError("memory runtime activation failed") from exc

    async def _activate_artifact_candidate(
        self,
        candidate: MemoryArtifactCandidate,
        root_state: MemoryProviderRootState,
        commit: Callable[[], None],
        rollback: Callable[[], None],
    ) -> None:
        """Cut over a verified pointer while preserving the prior runtime on failure."""

        async with self._reconcile_lock:
            async with self.module.lifecycle():
                if self._closing:
                    raise MemoryRuntimeActivationError("memory controller lifecycle is unavailable")
                if self._maintenance_open():
                    self.module.pause_claims()
                    raise MemoryRuntimeActivationError("memory clear recovery is required")
                if not await self.module.quiesce_claims():
                    self.module.resume_claims()
                    raise MemoryRuntimeActivationError("memory worker could not pause")
                async with self.module.provider_root_lifecycle():
                    meta = None
                    root_rollback: ProviderRootRollback | None = None
                    try:
                        await self._stop_worker()
                        if self._process is not None:
                            await self._process.stop()
                            self._process = None
                        self._process_records_calls = False
                        if root_state.exists:
                            meta = await asyncio.to_thread(self._store.get_meta)
                            if meta is None:
                                raise MemoryRuntimeActivationError("memory provider root metadata is missing")
                        if root_state.exists and root_state.empty and meta is not None:
                            root_rollback = await run_blocking(
                                self._provider_root_owner.activate_empty_format,
                                meta,
                                candidate,
                            )
                        commit()
                        result = await self._reconcile_locked(
                            self._config,
                            claims_already_paused=True,
                            skip_embedding_guard=self._config.recovery_intent is None,
                            resume_claims_on_failure=False,
                        )
                        if result.get("ok") is not True:
                            raise MemoryRuntimeActivationError("candidate runtime reconciliation failed")
                        self._restart_config = deepcopy(self._config)
                        return
                    except (Exception, asyncio.CancelledError) as activation_error:
                        try:
                            rollback()
                            if root_rollback is not None:
                                await run_blocking(root_rollback.rollback)
                            rollback_result = await self._reconcile_locked(
                                self._config,
                                claims_already_paused=True,
                                skip_embedding_guard=self._config.recovery_intent is None,
                                resume_claims_on_failure=False,
                            )
                            if rollback_result.get("ok") is not True:
                                raise MemoryRuntimeActivationError("previous runtime reconciliation failed")
                            self._restart_config = deepcopy(self._config)
                        except Exception as rollback_error:
                            self._runtime_error = "memory_runtime_install_failed"
                            raise MemoryRuntimeActivationError("memory runtime rollback failed") from rollback_error
                        if isinstance(activation_error, asyncio.CancelledError):
                            raise
                        raise MemoryRuntimeActivationError("memory runtime activation failed") from activation_error

    async def _stop_sidecar_for_clear(self) -> None:
        """Compatibility alias for the non-destructive clear quiesce step."""

        await self._quiesce_for_clear()

    async def _current_sidecar_ready(self, generation: int) -> None:
        """Accept the lifecycle module's semantic current-sidecar event."""

        async with self._reconcile_lock:
            async with self.module.lifecycle():
                snapshot = self._sidecar.snapshot()
                if not self._sidecar_ready_is_current(snapshot, generation):
                    return
                if self._maintenance_open():
                    self.module.pause_claims()
                    return
                if not self._sidecar_ready_is_current(self._sidecar.snapshot(), generation):
                    return
                self._runtime_error = None
                self.module.resume_claims()
                self._ensure_worker()

    def _schedule_sidecar_ready(self, process: EverOSProcessPort) -> None:
        """Compatibility bridge for legacy direct-supervisor test fixtures."""

        if self._closing:
            return
        snapshot = self._sidecar.snapshot()
        if process is not snapshot.process:
            return
        self._ready_activation_task = asyncio.create_task(
            self._current_sidecar_ready(snapshot.generation),
            name="memory-ready-activation",
        )

    def _sidecar_ready_is_current(
        self,
        snapshot: SidecarSnapshot,
        generation: int,
    ) -> bool:
        return bool(
            snapshot.generation == generation
            and snapshot.running
            and self._config.enabled
            and self._config.recovery_intent is None
            and self._restart_config.enabled
            and self._restart_config.recovery_intent is None
            and not self._artifact_installing
            and not self._closing
        )

    async def _processing_healthy(self) -> bool:
        """Answer the drain's processing gate without ever waiting on a peer probe.

        This gate runs inside the worker's drain lock, and Clear, clear recovery
        and reconciliation all fence that lock with a bounded budget. A
        processing probe spawns a short-lived child that can run for
        ``_PROCESSING_PROBE_TIMEOUT_SECONDS`` plus reaping, so queueing behind
        one already in flight pushed a single drain tick past every fence and
        stranded the durable clear marker. Single-flight is preserved without a
        lock: nothing is awaited between reading and setting the flag, so the
        loop cannot interleave two owners, and a second caller reads the last
        published verdict instead of blocking.
        """

        return await self._sidecar.processing_healthy()

    async def _probe_processing(self, python: Path, config: MemoryConfig) -> bool:
        """Probe a candidate configuration on a throwaway child.

        Deliberately shares no state with ``_processing_healthy``: this probe
        never touches the supervised child, and ``_reconcile_lock`` already
        serializes it. One shared lock made a settings save and the drain loop
        wait on each other -- the drain past its lifecycle fences, and the
        reconcile without any bound at all while holding the module lifecycle
        lock every read and Clear needs.
        """

        return await self._sidecar.probe(python, _process_settings(config))

    def _ensure_worker(self) -> None:
        if self._maintenance_open():
            self.module.pause_claims()
            return
        if self._worker_task is None or self._worker_task.done():
            self.module.begin_activation()
            self._worker_task = asyncio.create_task(self._drain_loop(), name="memory-drain")

    async def _stop_worker(self) -> None:
        task = self._worker_task
        if task is None:
            return
        task.cancel()
        # ``gather(return_exceptions=True)`` absorbs the worker's expected
        # cancellation, so any cancellation raised by shield belongs to this
        # caller. This works on Python 3.10 without ``Task.cancelling()``.
        settlement = asyncio.gather(task, return_exceptions=True)
        caller_cancellation: asyncio.CancelledError | None = None
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as error:
                caller_cancellation = caller_cancellation or error
        try:
            worker_result = settlement.result()[0]
        finally:
            if self._worker_task is task:
                self._worker_task = None
        if isinstance(worker_result, BaseException) and not isinstance(
            worker_result,
            asyncio.CancelledError,
        ):
            raise worker_result
        if caller_cancellation is not None:
            raise caller_cancellation

    def _ensure_call_log_retention(self) -> None:
        if self._recorder_health.get("reason") != "call_log_corrupt":
            self._sidecar.handoff_to_host_retention()

    async def _stop_call_log_retention(self) -> None:
        await self._sidecar.stop_host_retention()

    async def _maintain_call_log_once(self) -> tuple[bool, str | None]:
        async with self._reconcile_lock:
            if self._module is None:
                return await self._run_call_log_maintenance()
            async with self.module.lifecycle():
                return await self._run_call_log_maintenance()

    async def _run_call_log_maintenance(self) -> str | None:
        try:
            return await run_blocking(maintain_call_log, self._call_log_db_path)
        except Exception:
            return "writer_failures"

    def _call_log_exists(self) -> bool:
        try:
            self._call_log_db_path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _reset_recorder_health_unless_corrupt(self) -> None:
        if self._recorder_health.get("reason") != "call_log_corrupt":
            self._set_recorder_health_disabled()

    def _set_recorder_health_disabled(self) -> None:
        self._update_recorder_health(_RECORDER_DISABLED)

    async def _drain_loop(self) -> None:
        while self._config.enabled:
            try:
                await self.module.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Memory drain activation failed; retrying recovery")
                await self._recover_failed_drain()
            await asyncio.sleep(1.0)

    async def _recover_failed_drain(self) -> None:
        """Quiesce detached session work before rotating the worker lease."""

        while self._config.enabled:
            try:
                if await self.module.quiesce_claims():
                    break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Memory drain quiescence failed; retrying")
            await asyncio.sleep(1.0)
        else:
            return

        async with self._reconcile_lock:
            if not self._config.enabled or self._maintenance_open():
                return
            # A concurrent reconcile may have resumed claims while this task
            # waited for lifecycle ownership. Fence and join that newer work.
            while self._config.enabled:
                try:
                    if await self.module.quiesce_claims():
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Memory drain quiescence failed; retrying")
                await asyncio.sleep(1.0)
            else:
                return

            self.module.begin_activation(new_lease=True)
            while self._config.enabled:
                try:
                    # Claims remain paused, so this pass can only run boot recovery.
                    await self.module.drain()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Memory drain activation failed; retrying recovery")
                    await asyncio.sleep(1.0)
                    continue
                if not self._maintenance_open():
                    self.module.resume_claims()
                return

    def _data_exists(self) -> bool:
        """Return a conservative status projection of vector-bearing state."""

        try:
            return self._provider_data_exists_strict()
        except Exception:
            return True

    def _provider_data_exists_strict(self) -> bool:
        """Inspect all vector-bearing state, raising when it cannot be proven empty."""

        root_has_data = self._provider_root_owner.has_data()
        stats = self._store.queue_stats()
        return bool(root_has_data or stats.pending or stats.processing or stats.dead or self._store.has_provider_data_history())

    def _settle_rebuild_intent(self, candidate: MemoryConfig) -> MemoryConfig | None:
        """Clear a rebuild intent when the durable vector-space identity still matches.

        Compare-and-swap under the cross-process Memory config transaction so a
        newer confirmed candidate from the UI process cannot be clobbered by a
        stale Controller snapshot. Non-identity fields (API keys, LLM settings)
        may change under the marker without invalidating a completed rebuild.
        """

        def settle(current: MemoryConfig) -> MemoryConfig:
            if current.recovery_intent != "rebuild":
                raise ValueError("rebuild intent is no longer pending")
            if not _same_embedding_identity(current, candidate):
                raise ValueError("embedding identity changed during rebuild")
            return replace(current, recovery_intent=None)

        try:
            persisted = atomic_update_memory(settle)
            return persisted.memory
        except Exception:
            return None


def _provider_kwargs(config: MemoryConfig) -> dict[str, str | None]:
    rerank = config.processing.rerank
    return {
        "llm_base_url": config.processing.llm.base_url,
        "llm_model": config.processing.llm.model,
        "llm_api_key": config.processing.llm.api_key,
        "embedding_base_url": config.processing.embedding.base_url,
        "embedding_model": config.processing.embedding.model,
        "embedding_api_key": config.processing.embedding.api_key,
        "rerank_base_url": rerank.base_url if rerank else None,
        "rerank_model": rerank.model if rerank else None,
        "rerank_api_key": rerank.api_key if rerank else None,
    }


def _active_compatible_root_formats(artifact_manager: MemoryArtifactPort) -> tuple[str, ...]:
    """Read the active artifact's root formats, tolerating a broken pointer.

    The port guarantees the method exists, so only a raising or malformed
    implementation needs guarding here.
    """

    try:
        values = artifact_manager.compatible_provider_root_formats()
    except Exception:
        return ()
    if not isinstance(values, (set, frozenset, list, tuple)):
        return ()
    return tuple(value for value in values if isinstance(value, str))


def _embedding_configuration_changed(current: MemoryConfig, candidate: MemoryConfig) -> bool:
    """Compare only settings that define the embedding vector space."""

    current_embedding = current.processing.embedding
    candidate_embedding = candidate.processing.embedding
    return (
        current_embedding.base_url != candidate_embedding.base_url
        or current_embedding.model != candidate_embedding.model
    )


def _same_embedding_identity(current: MemoryConfig, candidate: MemoryConfig) -> bool:
    """Return whether two configs share the same embedding vector-space identity."""

    return not _embedding_configuration_changed(current, candidate)


def _process_settings(
    config: MemoryConfig,
    *,
    call_log_db_path: Path | None = None,
) -> EverOSProcessSettings:
    return EverOSProcessSettings(
        **_provider_kwargs(config),
        call_log_db_path=call_log_db_path,
    )


def _rebuild_settings_usable(settings: EverOSProcessSettings) -> bool:
    """Return whether the candidate embedding endpoint is complete enough to rebuild."""

    return all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_api_key,
        )
    )


def _memory_processing_complete(config: MemoryConfig) -> bool:
    """Return whether both configured processing providers can be contacted."""

    return config.processing.llm.complete() and config.processing.embedding.complete()


def _rebuild_public_result(result: RebuildProcessResult) -> dict[str, Any]:
    """Map a closed child result to the public rebuild response shape."""

    if result is RebuildProcessResult.COMPLETED:
        return {"ok": True, "result": "completed"}
    if result is RebuildProcessResult.ROOT_BUSY:
        return {
            "ok": False,
            "error": "memory_rebuild_root_busy",
            "result": "root_busy",
        }
    if result is RebuildProcessResult.INTERRUPTED:
        return {
            "ok": False,
            "error": "memory_rebuild_failed",
            "result": "interrupted",
        }
    if result is RebuildProcessResult.TIMED_OUT:
        return {
            "ok": False,
            "error": "memory_rebuild_failed",
            "result": "timed_out",
        }
    return {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }


def _repair_process_result(result: SyncProcessResult) -> dict[str, Any]:
    """Map one closed sync child result without exposing process details."""

    if result is SyncProcessResult.ALREADY_RUNNING:
        return {
            "ok": False,
            "error": "memory_operation_in_progress",
            "result": "failed",
        }
    if result is SyncProcessResult.INTERRUPTED:
        closed = "interrupted"
    elif result is SyncProcessResult.TIMED_OUT:
        closed = "timed_out"
    else:
        closed = "failed"
    return {
        "ok": False,
        "error": "memory_repair_failed",
        "result": closed,
    }


def _runtime_error_for_status(status: dict[str, Any]) -> str:
    reason = str(status.get("reason") or "")
    if "unsupported" in reason or "version" in reason:
        return "memory_runtime_unsupported"
    if "install" in reason or "checksum" in reason or "prepare" in reason:
        return "memory_runtime_install_failed"
    return "memory_runtime_missing"


def _result_payload(result: MemoryResult | RecallResult) -> dict[str, Any]:
    if isinstance(result, OperationFailed):
        return {"status": result.status, "error": result.error}
    if isinstance(result, MemoryItems):
        return {
            "status": result.status,
            "items": [memory_item_payload(item) for item in result.items],
            "warnings": list(result.warnings),
        }
    if isinstance(result, RecallItems):
        return {
            "status": result.status,
            "items": [memory_item_payload(item) for item in result.items],
            "warnings": list(result.warnings),
            "requested_mode": result.requested_mode,
            "effective_mode": result.effective_mode,
            "source": result.source,
            "current_session_overlay": result.current_session_overlay,
            "watermark_ms": result.watermark_ms,
            "freshness": result.freshness,
        }
    return {"status": "failed", "error": "memory_processing_failed"}


def _merge_search_items(items: list[MemoryItem], *, limit: int) -> tuple[MemoryItem, ...]:
    ordered = sorted(
        items,
        key=lambda item: (
            item.date is None,
            item.date or "",
            item.kind,
            item.text,
            item.project or "",
        ),
        reverse=False,
    )
    # date desc: invert the date key by sorting then reversing date-bearing first
    dated = [item for item in ordered if item.date is not None]
    undated = [item for item in ordered if item.date is None]
    dated.sort(key=lambda item: item.date or "", reverse=True)
    merged: list[MemoryItem] = []
    seen: set[tuple[str, str, str | None, str | None]] = set()
    for item in (*dated, *undated):
        key = (item.kind, item.text, item.date, item.project)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= limit:
            break
    return tuple(merged)


def _memory_list_catalog_fingerprint(
    principal_id: str,
    projects: tuple[str, ...],
) -> str:
    material = json.dumps(
        {
            "principal": principal_id,
            "projects": list(projects),
            "sort": "timestamp:desc,project:id:asc",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _encode_memory_list_cursor(
    fingerprint: str,
    boundaries: dict[str, tuple[str, str] | None],
    page_hints: dict[str, int],
    total_hints: dict[str, int | None],
) -> str:
    if (
        set(page_hints) != set(boundaries)
        or set(total_hints) != set(boundaries)
        or any(
        isinstance(page_hint, bool)
        or not isinstance(page_hint, int)
        or not 1 <= page_hint <= _MEMORY_LIST_PROVIDER_MAX_PAGE
        or (boundaries[project_id] is None and page_hint != 1)
        for project_id, page_hint in page_hints.items()
        )
        or any(
            (boundary is None and total_hints[project_id] is not None)
            or (
                boundary is not None
                and (
                    isinstance(total_hints[project_id], bool)
                    or not isinstance(total_hints[project_id], int)
                    or total_hints[project_id] < 0
                )
            )
            for project_id, boundary in boundaries.items()
        )
    ):
        raise ValueError("invalid Memory list page hints")
    encoded_boundaries = {
        project_id: (
            {
                "t": boundary[0],
                "i": _encode_memory_list_boundary_id(boundary[1]),
                "p": page_hints[project_id],
                "n": total_hints[project_id],
            }
            if boundary is not None
            else None
        )
        for project_id, boundary in boundaries.items()
    }
    raw = json.dumps(
        {
            "v": _MEMORY_LIST_CURSOR_VERSION,
            "f": fingerprint,
            "b": encoded_boundaries,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(token.encode("ascii")) > MEMORY_LIST_CURSOR_MAX_BYTES:
        raise ValueError("Memory list cursor is too large")
    return token


def _decode_memory_list_cursor(
    cursor: str | None,
    *,
    projects: tuple[str, ...],
    fingerprint: str,
) -> tuple[
    dict[str, tuple[str, str] | None],
    dict[str, int],
    dict[str, int | None],
]:
    if cursor is None:
        return (
            {project_id: None for project_id in projects},
            {project_id: 1 for project_id in projects},
            {project_id: None for project_id in projects},
        )
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor.encode("utf-8")) > MEMORY_LIST_CURSOR_MAX_BYTES
    ):
        raise ValueError("invalid Memory list cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
    except (UnicodeEncodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid Memory list cursor") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "f", "b"}
        or payload.get("v") != _MEMORY_LIST_CURSOR_VERSION
        or payload.get("f") != fingerprint
        or not isinstance(payload.get("b"), dict)
        or set(payload["b"]) != set(projects)
    ):
        raise ValueError("invalid Memory list cursor")
    boundaries: dict[str, tuple[str, str] | None] = {}
    page_hints: dict[str, int] = {}
    total_hints: dict[str, int | None] = {}
    for project_id in projects:
        value = payload["b"][project_id]
        if value is None:
            boundaries[project_id] = None
            page_hints[project_id] = 1
            total_hints[project_id] = None
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {"t", "i", "p", "n"}
            or not isinstance(value.get("t"), str)
            or len(value["t"]) > 64
            or isinstance(value.get("p"), bool)
            or not isinstance(value.get("p"), int)
            or not 1 <= value["p"] <= _MEMORY_LIST_PROVIDER_MAX_PAGE
            or isinstance(value.get("n"), bool)
            or not isinstance(value.get("n"), int)
            or value["n"] < 0
        ):
            raise ValueError("invalid Memory list cursor")
        try:
            _memory_list_instant(value["t"])
            boundary_id = _decode_memory_list_boundary_id(value.get("i"))
        except ValueError:
            raise ValueError("invalid Memory list cursor") from None
        boundaries[project_id] = (value["t"], boundary_id)
        page_hints[project_id] = value["p"]
        total_hints[project_id] = value["n"]
    return boundaries, page_hints, total_hints


def _encode_memory_list_boundary_id(value: str) -> str:
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid Memory list boundary ID") from None
    if not raw or len(raw) > 128 or "\x00" in value:
        raise ValueError("invalid Memory list boundary ID")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_memory_list_boundary_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 171:
        raise ValueError("invalid Memory list boundary ID")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        decoded = raw.decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        raise ValueError("invalid Memory list boundary ID") from None
    if _encode_memory_list_boundary_id(decoded) != value:
        raise ValueError("invalid Memory list boundary ID")
    return decoded


def _memory_list_instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("invalid Memory list timestamp")
    return instant.astimezone(timezone.utc)


def _order_project_memory_list_items(
    items: Iterable[MemoryListItem],
) -> list[MemoryListItem]:
    ordered = sorted(items, key=lambda item: item.id)
    ordered.sort(
        key=lambda item: _memory_list_instant(item.timestamp),
        reverse=True,
    )
    return ordered


def _memory_list_after_boundary(
    item: MemoryListItem,
    boundary: tuple[str, str] | None,
) -> bool:
    if boundary is None:
        return True
    item_instant = _memory_list_instant(item.timestamp)
    boundary_instant = _memory_list_instant(boundary[0])
    return item_instant < boundary_instant or (
        item_instant == boundary_instant and item.id > boundary[1]
    )
