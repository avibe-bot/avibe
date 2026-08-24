"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import errno
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
from typing import Any, Literal

from config import paths
from config.v2_config import (
    MemoryConfig,
)
from core.memory.artifact import (
    EVEROS_VERSION,
    MemoryArtifactCandidate,
    MemoryArtifactPort,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
    get_memory_artifact_manager,
)
from core.memory.attachments import IM_ATTACHMENT_CAPTURE_PLATFORMS
from core.memory.blocking import run_blocking
from core.memory.confined_filesystem import ConfinedRoot, required_no_follow_flag
from core.memory.everos import (
    EverOSPort,
    MemoryProviderFailure,
    ProviderHealthSnapshot,
)
from core.memory.everos_insight import MemoryInsightPaths, MemoryInsightReader
from core.memory.module import MemoryModule, MemorySessionLifecycleBusyError
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from core.memory.process import (
    EverOSProcess,
    EverOSProcessFactory,
    EverOSProcessPort,
    EverOSProcessSettings,
    RecordedSidecarReaper,
)
from core.memory.sidecar_lifecycle import MemorySidecarLifecycle, SidecarSnapshot
from core.memory.processing_record import (
    AnomalyProjection,
    FailureLogObservation,
    MaintenanceProjection,
    MaintenanceObservation,
    MaintenanceResult,
    MemoryProcessingRecord,
    MemoryProcessingRecordPort,
    ProcessingRecordSummary,
    ProcessingSourceObservations,
    RuntimeHealthObservation,
    RuntimeHealthProjection,
    SourceObservation,
)
from core.memory.provider_root import (
    ProviderRoot,
    ProviderRootError,
    ProviderRootMetadata,
    ProviderRootRollback,
)
from core.memory.project_ids import (
    DEFAULT_MEMORY_PROJECT_ID,
    MEMORY_SEARCH_ALL_PROJECTS,
)
from core.memory.store import MemoryStore, is_principal_id
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
logger = logging.getLogger(__name__)

ProcessingEvent = Callable[
    [Literal["fault", "recovered"], Literal["credential", "engine"] | None, str, int],
    Awaitable[bool],
]


ARTIFACT_ACTIVATION_TIMEOUT_SECONDS = 90.0
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
    retired: bool
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
        if self.retired or self.transition_active or self.closing:
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
        if self.retired or self.transition_active or self.closing:
            return "busy"
        return None

    def same_lifecycle(self, other: _ProcessingRuntimeSnapshot) -> bool:
        return (
            self.generation == other.generation
            and self.transition_active == other.transition_active
            and self.enabled == other.enabled
            and self.store_available == other.store_available
            and self.retired == other.retired
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


def _source_observation_payload(source: SourceObservation) -> dict[str, Any]:
    return {
        "status": source.status,
        "observed_at": source.observed_at,
        "reason": source.reason,
    }


def _runtime_health_payload(result: RuntimeHealthProjection) -> dict[str, Any]:
    health = None if result.health is None else result.health.payload()
    if health is not None:
        health.pop("cascade", None)
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
        "can_delete_data": result.can_delete_data,
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
            "memcells": _source_observation_payload(summary.sources.memcells),
            "runs": _source_observation_payload(summary.sources.runs),
            "semantic": _source_observation_payload(summary.sources.semantic),
        },
        "anomalies": _anomaly_projection_payload(summary.anomalies),
        "maintenance": _maintenance_projection_payload(summary.maintenance),
    }


class _UnavailableMemoryModule:
    """Capture sink for a runtime whose store could not be opened.

    ``MemoryRuntime.module`` is only ever used to capture, so this narrow adapter
    is the whole seam, not a stand-in for the complete ``MemoryModule`` interface.
    """

    async def capture(
        self,
        _request: Any,
        *,
        source_lease: Any = None,
    ) -> OperationFailed:
        del source_lease
        return OperationFailed(error="memory_store_unavailable")


_UNAVAILABLE_MODULE = _UnavailableMemoryModule()


class MemoryStoreUnavailableError(RuntimeError):
    """Raised when the local Memory store cannot be used safely."""


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
    """Own local Memory state, sidecar reconciliation, and volatile capture."""

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
        self._wake_config = deepcopy(config)
        self._filesystem_root = ConfinedRoot.from_home(
            effective_home or paths.get_vibe_remote_dir()
        )
        self._effective_home = self._filesystem_root.physical_home
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
        self._needs_repair_reason: str | None = (
            "memory_legacy_recovery_required"
            if config.legacy_needs_repair
            else None
        )
        self._processing_lifecycle_generation = 0
        self._reconcile_lock = _LifecycleGenerationLock(
            self._advance_processing_lifecycle
        )
        self._wake_task: asyncio.Task[dict[str, Any]] | None = None
        self._ready_activation_task: asyncio.Task[None] | None = None
        self._artifact_activation_task: asyncio.Task[None] | None = None
        self._closing = False
        self._activation_loop: asyncio.AbstractEventLoop | None = None
        self._artifact_installing = False
        self._retired = False
        self._closed = False
        self._store: MemoryStore | None = None
        self._module: MemoryModule | None = None
        self._store_error: Exception | None = None
        self._insight_reader_override = insight_reader
        self._insight_reader: MemoryInsightReader | None = None
        self._sidecar = MemorySidecarLifecycle(
            self._process_factory,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            socket_path=self._socket_path,
            on_current_sidecar_ready=self._current_sidecar_ready,
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
            opened = store or MemoryStore(effective_home=self._effective_home)
            self._artifact_manager.set_provider_root(self._provider_root)
            module = MemoryModule(
                opened,
                self._provider,
                enabled=lambda: self._config.enabled,
                provider_root=self._provider_root,
                provider_root_owner=self._provider_root_owner,
                processing_event=self._processing_event,
                ambiguous_stop_reap=self._settle_ambiguous_provider_outcome,
                effective_home=self._effective_home,
            )
        except Exception as exc:
            self._store_error = exc
            if _local_data_failure_requires_repair(exc):
                self._needs_repair_reason = "memory_local_data_unusable"
                self._runtime_error = self._needs_repair_reason
            else:
                self._runtime_error = _degraded_runtime_reason(exc)
            logger.exception("Memory store initialization failed; continuing with Memory unavailable")
            return False
        self._store = opened
        self._module = module
        if self._legacy_clear_state_exists(opened):
            self._needs_repair_reason = "memory_legacy_recovery_required"
        if self._needs_repair_reason is not None:
            module.pause_claims()
        self._store_error = None
        self._configure_insight_reader(self._config)
        self._artifact_manager.set_activation_coordinator(self._coordinate_artifact_activation)
        return True

    def _legacy_clear_state_exists(self, store: MemoryStore) -> bool:
        """Consume released Clear workflow state as a repair classification only."""

        try:
            if store.clear_in_progress():
                return True
        except Exception as exc:
            self._runtime_error = _degraded_runtime_reason(exc)
            return False
        return any(
            os.path.lexists(self._effective_home / relative_path)
            for relative_path in (
                "state/memory/clear-intent.json",
                "state/memory/clear-journal.sqlite",
                "state/memory/clear-snapshots",
                "state/memory/backup-restore-journal.sqlite",
                "state/memory/backups",
            )
        )

    @property
    def available(self) -> bool:
        """Whether the local store opened. False keeps every read closed."""

        return self._module is not None and not self._retired

    @property
    def needs_repair(self) -> bool:
        """Whether local mutable data is known unusable or incompatible."""

        return self._needs_repair_reason is not None

    @property
    def needs_repair_reason(self) -> str | None:
        return self._needs_repair_reason

    def mark_needs_repair(self, reason: str) -> None:
        """Keep a failed destructive operation closed until it is rerun."""

        self._needs_repair_reason = reason
        self._runtime_error = None
        self.module.pause_claims()

    async def settle_after_data_loss(self) -> None:
        """Rotate data authority after the owned process has stopped.

        Stable scope identity and the project catalog stay in place. The
        Controller calls this only inside its destructive operation lease.
        """

        if not self._closed or self._store is None:
            raise RuntimeError("Memory runtime must be closed before data settlement")
        await asyncio.to_thread(self._store.settle_after_data_loss)

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

    # Remaining Runtime paths still read these projections while their own
    # lifecycle work is migrated. Ownership remains in ``_sidecar``.
    @property
    def _process(self) -> EverOSProcessPort | None:
        return self._sidecar.snapshot().process

    @_process.setter
    def _process(self, process: EverOSProcessPort | None) -> None:
        self._sidecar._replace_for_runtime(process)

    def _processing_record_port(self) -> MemoryProcessingRecordPort:
        return MemoryProcessingRecordPort(
            resolve_operator=self._processing_record_operator,
            observe_maintenance=self._processing_record_maintenance_observation,
            observe_health=self._processing_record_health,
            failure_log=self._processing_record_failure_log,
            observe_sources=self._processing_record_sources,
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
            retired=bool(module and module.retired),
            closing=self._closing,
            process=process,
            launch_token=sidecar.launch_token,
            process_running=bool(process and process.running),
            runtime_error=self._runtime_error,
        )


    def _configure_insight_reader(self, config: MemoryConfig) -> None:
        if self._insight_reader_override is not None:
            self._insight_reader = self._insight_reader_override
            return
        if self._store is None:
            self._insight_reader = None
            return
        processing = config.runtime_processing()
        rerank = processing.rerank
        multimodal = processing.multimodal
        base_urls = tuple(
            value
            for value in (
                processing.llm.base_url,
                processing.embedding.base_url,
                rerank.base_url if rerank else None,
                multimodal.base_url if multimodal else None,
            )
            if value
        )
        exact_redaction_values = tuple(
            value
            for value in (
                processing.llm.api_key,
                processing.embedding.api_key,
                rerank.api_key if rerank else None,
                multimodal.api_key if multimodal else None,
            )
            if value
        )
        self._insight_reader = MemoryInsightReader(
            MemoryInsightPaths(self._provider_root),
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
                recovery = RecordedSidecarReaper(
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


    async def reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Apply persisted config without restarting the Avibe service."""

        if self._retired:
            return {"ok": False, "error": "memory_operation_in_progress"}
        return await self._reconcile(config)

    async def _reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Run one reconciliation owned by the public lifecycle operation."""

        # Before every early return below, because a boot that never launches a
        # sidecar is exactly the boot that may face one from the run before it.
        # Takes and releases the reconcile lock itself; the lock this method
        # acquires later is a separate, sequential acquisition.
        await self._reap_recorded_sidecar_if_unowned()
        if not self.available:
            # A transient store failure must not close Memory forever: every
            # reconciliation is another chance to open it.
            self._config = config
            if not config.enabled:
                async with self._reconcile_lock:
                    self._wake_config = deepcopy(config)
                    return {"ok": True, "state": "disabled"}
            if not self._open_store():
                logger.warning("Memory store remains unavailable during reconciliation")
                return {
                    "ok": False,
                    "state": "needs_repair" if self.needs_repair else "degraded",
                    "error": self._runtime_error or "memory_store_unavailable",
                }
        async with self._reconcile_lock:
            self._activation_loop = asyncio.get_running_loop()
            if self._artifact_installing:
                return {"ok": False, "error": "memory_runtime_install_failed"}
            async with self.module.lifecycle():
                result = await self._reconcile_locked(config)
                if result.get("ok") is True:
                    self._wake_config = deepcopy(config)
                return result

    async def _disable_locked(self, config: MemoryConfig) -> dict[str, Any]:
        """Stop every active Memory component without consulting maintenance state."""

        self.module.pause_claims()
        self._config = config
        self._configure_insight_reader(config)
        self._provider = EverOSPort(self._socket_path)
        self.module.replace_provider(self._provider)
        await self._close_writer()
        if self._process is not None:
            await self._process.stop()
            self._process = None
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

        if self.needs_repair:
            self.module.pause_claims()
            await self._close_writer()
            await self._sidecar.stop()
            self._config = deepcopy(config)
            self._wake_config = deepcopy(config)
            self._runtime_error = self._needs_repair_reason
            return {
                "ok": False,
                "state": "needs_repair",
                "error": self._needs_repair_reason,
            }

        cloud_identity_changed = bool(
            config.cloud_runtime_selected()
            and config.cloud.embedding_identity
            and config.cloud.applied_embedding_identity
            and config.cloud.embedding_identity
            != config.cloud.applied_embedding_identity
        )
        if cloud_identity_changed:
            self.module.pause_claims()
            await self._close_writer()
            await self._sidecar.stop()
            self._config = deepcopy(config)
            self._wake_config = deepcopy(config)
            self._runtime_error = "memory_loss_confirmation_required"
            return {
                "ok": False,
                "state": "degraded",
                "error": self._runtime_error,
            }

        if config.cloud_runtime_selected() and config.runtime_source() == "unavailable":
            # A managed scope losing either half of the chat+embedding pair is
            # a pause, never a fallback to saved custom providers. Capture can
            # keep queuing while claims and the old sidecar stay fenced.
            self.module.pause_claims()
            await self._close_writer()
            await self._sidecar.stop()
            self._config = deepcopy(config)
            self._wake_config = deepcopy(config)
            self._configure_insight_reader(config)
            self._provider = EverOSPort(self._socket_path)
            self.module.replace_provider(self._provider)
            self._runtime_error = "memory_capability_unavailable"
            return {
                "ok": True,
                "state": "degraded",
                "reason": self._runtime_error,
            }

        previous_config = deepcopy(self._config)
        embedding_changed = (
            not skip_embedding_guard
            and _embedding_configuration_changed(self._config, config)
        )
        claims_paused = claims_already_paused
        if embedding_changed:
            # Fence and join volatile writer work before inspecting provider
            # state, so no old-embedding call can cross this boundary.
            if not await self.module.quiesce_claims():
                if resume_claims_on_failure:
                    self._defer_wake_until_writer_closed()
                self._runtime_error = "memory_loss_confirmation_required"
                return {"ok": False, "state": "degraded", "error": self._runtime_error}
            claims_paused = True
            if not await self._embedding_change_is_admissible(self._config, config):
                restored = False
                if resume_claims_on_failure:
                    restored = await self._restore_provisional_claims(previous_config)
                if not restored:
                    self._runtime_error = "memory_loss_confirmation_required"
                return {
                    "ok": False,
                    "state": "degraded",
                    "error": "memory_loss_confirmation_required",
                }

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
            if not self._sidecar.snapshot().retains_active_config:
                # No retained supervisor can run now or relaunch with the prior
                # settings, including one that exhausted its wake budget.
                # Retain the desired config so a first artifact install can
                # activate it without waiting for another reconciliation.
                self._config = config
                self._runtime_error = error
            if claims_paused and resume_claims_on_failure:
                await self._restore_provisional_claims(previous_config)
            return {"ok": False, "error": error}
        if not await self._probe_processing(python, config):
            error = "memory_processing_failed"
            if not (self._process and self._process.running):
                self._runtime_error = error
            if claims_paused and resume_claims_on_failure:
                await self._restore_provisional_claims(previous_config)
            return {"ok": False, "error": error}

        # Every enabled reconciliation receives a fresh process. Endpoint,
        # model, and key changes belong exclusively in its allowlisted child
        # environment and must never leave an old sidecar running.
        if not claims_paused and not await self.module.quiesce_claims():
            if resume_claims_on_failure:
                self._defer_wake_until_writer_closed()
            self._runtime_error = "memory_runtime_busy"
            return {"ok": False, "state": "degraded", "error": self._runtime_error}
        await self._close_writer()
        await self._sidecar.stop()

        self._config = config
        self._configure_insight_reader(config)
        self._provider = candidate_provider
        self.module.replace_provider(self._provider)
        try:
            meta = await asyncio.to_thread(self._store.ensure_meta)
            active_metadata = self._active_provider_root_metadata()
            await run_blocking(
                self._provider_root_owner.ensure,
                meta,
                active_metadata,
            )
        except Exception as exc:
            if _local_data_failure_requires_repair(exc):
                self._needs_repair_reason = "memory_local_data_unusable"
                self._runtime_error = self._needs_repair_reason
                state = "needs_repair"
            else:
                self._runtime_error = _degraded_runtime_reason(exc)
                state = "degraded"
            self.module.pause_claims()
            return {"ok": False, "state": state, "error": self._runtime_error}

        settings = _process_settings(config)
        try:
            started = await self._sidecar.start(
                python,
                settings,
                provider_root_guard=lambda: self._provider_root_owner.require_owned(
                    meta,
                    active_metadata,
                ),
            )
        except BaseException:
            raise
        if not started:
            self._runtime_error = "memory_sidecar_unavailable"
            return {"ok": False, "state": "degraded", "error": self._runtime_error}
        self._runtime_error = None
        self.module.resume_claims()
        self._resume_writer()
        return {"ok": True, "state": "running"}

    async def _processing_record_maintenance_observation(
        self,
        operator_ref: str | None,
    ) -> MaintenanceObservation:
        del operator_ref
        return MaintenanceObservation(
            block_reason=(
                self._needs_repair_reason
                if self.needs_repair
                else (None if self.available else "memory_store_unavailable")
            ),
            can_delete_data=self.available and not self._closing,
        )

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
            # Per-call durable failure history was retired with the delivery
            # protocol. Keep the diagnostics capability explicit: unavailable
            # is distinct from an authorized empty result.
            return FailureLogObservation((), "memory_failure_history_unavailable")

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
                memcells=unavailable,
                runs=unavailable,
                semantic=unavailable,
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
                memcells=unavailable,
                runs=unavailable,
                semantic=unavailable,
            )
        return observation

    async def _processing_record_maintenance(
        self,
        operator_ref: str | None,
        observation: MaintenanceObservation,
    ) -> MaintenanceResult:
        del operator_ref
        data_exists = True
        if self.available:
            try:
                data_exists = await asyncio.to_thread(self._provider_data_exists_strict)
            except Exception:
                data_exists = True
        return MaintenanceResult(
            data_exists=data_exists,
            can_delete_data=observation.can_delete_data,
            error=observation.block_reason,
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
        payload = _runtime_health_payload(runtime)
        payload["state"] = self.runtime_state()
        payload["reason"] = (
            self._needs_repair_reason
            if self.needs_repair
            else self._runtime_error
        )
        payload["attachment_capture"] = {
            "status": _attachment_capture_status(
                self._config,
                runtime.source.status,
                payload.get("health"),
                bool(self._module and self._module.attachment_intake_enabled),
            ),
        }
        return payload

    def runtime_state(self) -> Literal[
        "disabled", "starting", "running", "degraded", "needs_repair"
    ]:
        if not self._config.enabled:
            return "disabled"
        if self.needs_repair:
            return "needs_repair"
        if self._artifact_installing or self._reconcile_lock.locked():
            return "starting"
        if self._process is not None and self._process.running and self._runtime_error is None:
            return "running"
        if self._runtime_error is None:
            return "starting"
        return "degraded"

    async def attachment_capture_status(
        self,
    ) -> Literal["ready", "not_configured", "unavailable"]:
        """Return the same fresh readiness projection used by Memory status."""

        runtime = await self._processing_record.read_status()
        payload = _runtime_health_payload(runtime)
        return _attachment_capture_status(
            self._config,
            runtime.source.status,
            payload.get("health"),
            bool(self._module and self._module.attachment_intake_enabled),
        )

    def attachment_capture_config_generation(self) -> int | None:
        """Return the stable explicit opt-in generation without probing providers."""

        snapshot = self._processing_runtime_snapshot()
        if (
            snapshot.transition_active
            or not self._config.enabled
            or not self._config.effective_multimodal_available()
            or self._module is None
            or not self._module.attachment_intake_enabled
        ):
            return None
        return snapshot.generation

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
            "can_delete_data": result.can_delete_data,
        }

    def principal_for_user_key(self, user_key: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.principal_for_user_key(user_key)

    def project_for_workdir(self, workdir: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.project_for_workdir(workdir)

    def offer_barrier(self, raw_session_id: str) -> str:
        """Offer a non-blocking provider barrier for lifecycle transitions."""

        if not self.available:
            return "disabled"
        return self.module.offer_barrier(raw_session_id)

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
            for page_number, snapshot in tuple(probes.items()):
                refreshed = await read_page(page_number)
                if isinstance(refreshed, OperationFailed):
                    return refreshed
                warnings.extend(refreshed.warnings)
                if refreshed != snapshot:
                    return (None, refreshed.total_count)
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
            warnings.extend(result.warnings)
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

    async def processing_record_entries_payload(
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
            lambda: reader.list_processing_records(
                (principal_id, project_id), cursor, limit
            )
        )

    async def processing_record_entry_payload(
        self,
        principal_id: str,
        project_id: str,
        memcell_id: str,
    ) -> dict[str, Any]:
        reader = self._insight_reader
        if not self.available or reader is None:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return await self._run_insight_read(
            lambda: reader.processing_record_detail(
                (principal_id, project_id), memcell_id
            )
        )

    async def _run_insight_read(
        self,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self.module.lifecycle():
            return await run_blocking(operation)

    async def install_artifact(self) -> dict[str, Any]:
        """Install or repair the admitted EverOS artifact."""

        lease = MemoryOperationLease(self._effective_home)
        try:
            await run_blocking(lease.acquire)
            return await self._install_artifact_with_lease()
        except MemoryOperationBusy:
            return {
                "ok": False,
                "reason": "memory_operation_in_progress",
                "download_error": None,
            }
        finally:
            await run_blocking(lease.release)

    async def _install_artifact_with_lease(self) -> dict[str, Any]:
        self._activation_loop = asyncio.get_running_loop()
        async with self._reconcile_lock:
            if self._artifact_installing:
                return {
                    "ok": False,
                    "reason": "memory_operation_in_progress",
                    "download_error": None,
                }
            if self._process is not None:
                return {
                    "ok": False,
                    "reason": "memory_runtime_install_requires_stopped_memory",
                    "download_error": None,
                }
            self._artifact_installing = True

        try:
            payload = await asyncio.to_thread(self._artifact_manager.ensure, force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Memory artifact install failed")
            return {
                "ok": False,
                "reason": "memory_runtime_install_failed",
                "download_error": None,
            }
        finally:
            async with self._reconcile_lock:
                self._artifact_installing = False

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

    async def wake(
        self,
        *,
        operation_lease_held: bool = False,
    ) -> dict[str, Any]:
        """Validate the artifact and non-destructively wake the existing root."""

        if self._closing or self._retired or self.needs_repair:
            return {
                "ok": False,
                "state": self.runtime_state(),
                "error": self._needs_repair_reason or "memory_operation_in_progress",
            }
        lease = MemoryOperationLease(self._effective_home)
        try:
            if not operation_lease_held:
                await run_blocking(lease.acquire)
            if not self.artifact_admitted():
                if self.available:
                    async with self._reconcile_lock, self.module.lifecycle():
                        self.module.pause_claims()
                        if not await self.module.quiesce_claims(timeout_seconds=5.0):
                            return {
                                "ok": False,
                                "state": "degraded",
                                "error": "memory_runtime_busy",
                            }
                        await self._close_writer()
                        try:
                            await self._sidecar.stop()
                        except Exception:
                            return {
                                "ok": False,
                                "state": "degraded",
                                "error": "memory_wake_failed",
                            }
                installed = await self._install_artifact_with_lease()
                if installed.get("ok") is not True:
                    self._runtime_error = str(
                        installed.get("reason") or "memory_wake_failed"
                    )
                    return {
                        "ok": False,
                        "state": "degraded",
                        "error": self._runtime_error,
                    }
            async with self._reconcile_lock:
                return await self._wake_locked()
        except MemoryOperationBusy:
            return {"ok": False, "error": "memory_operation_in_progress"}
        except Exception as exc:
            logger.exception("Memory wake failed")
            self._runtime_error = _degraded_runtime_reason(exc, default="memory_wake_failed")
            return {"ok": False, "state": "degraded", "error": self._runtime_error}
        finally:
            if not operation_lease_held:
                await run_blocking(lease.release)

    def _defer_wake_until_writer_closed(self) -> None:
        """Volatile recovery after writer cleanup; no workflow state is persisted."""

        task = self._wake_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._wake_after_writer_close(),
            name="memory-wake-after-writer-close",
        )
        self._wake_task = task
        task.add_done_callback(
            lambda completed: setattr(self, "_wake_task", None)
            if self._wake_task is completed
            else None
        )

    async def _wake_after_writer_close(self) -> dict[str, Any]:
        try:
            await self._close_writer()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Memory writer cleanup failed before deferred wake")
            self._runtime_error = "memory_wake_failed"
            return {"ok": False, "error": self._runtime_error}
        while self._artifact_installing and not self._closing:
            await asyncio.sleep(0)
        return await self.wake()

    async def preflight(self, config: MemoryConfig | None = None) -> dict[str, Any]:
        candidate = deepcopy(config or self._config)
        processing = candidate.runtime_processing()
        provider = EverOSPort(
            self._socket_path,
            llm_base_url=processing.llm.base_url,
            llm_model=processing.llm.model,
            llm_api_key=processing.llm.api_key,
            embedding_base_url=processing.embedding.base_url,
            embedding_model=processing.embedding.model,
            embedding_api_key=processing.embedding.api_key,
            rerank_base_url=(processing.rerank.base_url if processing.rerank else None),
            rerank_model=(processing.rerank.model if processing.rerank else None),
            rerank_api_key=(processing.rerank.api_key if processing.rerank else None),
            rerank_provider=(
                processing.rerank.rerank_provider() if processing.rerank else None
            ),
            multimodal_base_url=(
                processing.multimodal.base_url
                if processing.multimodal
                else None
            ),
            multimodal_model=(
                processing.multimodal.model
                if processing.multimodal
                else None
            ),
            multimodal_api_key=(
                processing.multimodal.api_key
                if processing.multimodal
                else None
            ),
        )
        return (await provider.preflight()).payload()

    async def _wake_locked(self) -> dict[str, Any]:
        """Wake the admitted sidecar while preserving the existing data root."""

        if self._closing or self._retired:
            return {"ok": False, "error": "memory_operation_in_progress"}
        if self.needs_repair:
            return {
                "ok": False,
                "state": "needs_repair",
                "error": self._needs_repair_reason,
            }
        self._activation_loop = asyncio.get_running_loop()
        if not self.available or self._store is None or self._module is None:
            return {
                "ok": False,
                "state": "degraded",
                "error": self._runtime_error or "memory_store_unavailable",
            }
        if self._artifact_installing:
            return {"ok": False, "error": "memory_operation_in_progress"}

        replay = deepcopy(self._wake_config)
        if not replay.enabled:
            return {"ok": False, "state": "disabled", "error": "memory_disabled"}
        python = await asyncio.to_thread(self._artifact_manager.resolve_python)
        if python is None:
            self._runtime_error = "memory_wake_failed"
            return {"ok": False, "state": "degraded", "error": self._runtime_error}

        async with self.module.lifecycle():
            self.module.pause_claims()
            try:
                quiesced = await self.module.quiesce_claims(timeout_seconds=5.0)
                if quiesced:
                    await self._close_writer()
            except Exception:
                quiesced = False
            if not quiesced:
                self._runtime_error = "memory_runtime_busy"
                return {"ok": False, "state": "degraded", "error": self._runtime_error}

            try:
                await self._sidecar.stop()
            except Exception:
                logger.exception("Memory wake could not prove the old sidecar stopped")
                self._runtime_error = "memory_wake_failed"
                return {"ok": False, "state": "degraded", "error": self._runtime_error}

            self._config = replay
            self._configure_insight_reader(replay)
            self._provider = EverOSPort(
                self._socket_path,
                processing_health_check=self._processing_healthy,
            )
            self.module.replace_provider(self._provider)
            try:
                meta = await asyncio.to_thread(self._store.ensure_meta)
                active_metadata = self._active_provider_root_metadata()
                await run_blocking(
                    self._provider_root_owner.ensure,
                    meta,
                    active_metadata,
                )
            except Exception as exc:
                if _local_data_failure_requires_repair(exc):
                    self._needs_repair_reason = "memory_local_data_unusable"
                    self._runtime_error = self._needs_repair_reason
                    state = "needs_repair"
                else:
                    self._runtime_error = _degraded_runtime_reason(exc)
                    state = "degraded"
                return {"ok": False, "state": state, "error": self._runtime_error}

            try:
                started = await self._sidecar.start(
                    python,
                    _process_settings(replay),
                    provider_root_guard=lambda: self._provider_root_owner.require_owned(
                        meta,
                        active_metadata,
                    ),
                )
            except Exception as exc:
                self._runtime_error = _degraded_runtime_reason(
                    exc,
                    default="memory_wake_failed",
                )
                return {"ok": False, "state": "degraded", "error": self._runtime_error}
            if not started:
                self._runtime_error = "memory_sidecar_unavailable"
                return {"ok": False, "state": "degraded", "error": self._runtime_error}

            readiness_error: str | None = None
            for delay in (0.0, 0.25, 0.5, 1.0):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await self._provider.health_snapshot()
                    readiness_error = None
                    break
                except MemoryProviderFailure as failure:
                    readiness_error = failure.error
                except Exception as exc:
                    readiness_error = _degraded_runtime_reason(exc)
            if readiness_error is not None:
                self.module.pause_claims()
                self._runtime_error = readiness_error
                return {"ok": False, "state": "degraded", "error": readiness_error}

            self._runtime_error = None
            self.module.resume_claims()
            self._resume_writer()
            return {"ok": True, "state": "running"}

    async def close(self) -> None:
        self._closing = True
        self._sidecar.close_ready_admission()
        self._activation_loop = None
        self._advance_processing_lifecycle()

        cancellation: asyncio.CancelledError | None = None
        wake = self._wake_task
        if wake is not None and wake is not asyncio.current_task():
            if not wake.done():
                wake.cancel()
            cancellation = await self._join_shutdown_task(wake)
            try:
                wake.result()
            except (Exception, asyncio.CancelledError):
                pass

        cleanup = asyncio.create_task(
            self._close_owned_runtime(),
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

    async def _close_owned_runtime(self) -> None:
        """Finish ordinary shutdown after runtime ownership is released."""

        cleanup_error: BaseException | None = None
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
                await self._close_writer()
            except BaseException as error:
                cleanup_error = error
            async with self._module.provider_root_lifecycle():
                try:
                    if (
                        self._retired
                        and not await self._module.quiesce_claims_for_destructive_reset()
                    ):
                        raise RuntimeError(
                            "Memory writer did not quiesce during retired close"
                        )
                    await self._module.close_writer()
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

        if root_state is None:
            raise MemoryRuntimeActivationError("memory provider root could not be inspected")

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
                if not await self.module.quiesce_claims():
                    self._runtime_error = "memory_runtime_install_failed"
                    self._defer_wake_until_writer_closed()
                    raise MemoryRuntimeActivationError("memory writer could not pause")
                async with self.module.provider_root_lifecycle():
                    meta = None
                    root_rollback: ProviderRootRollback | None = None
                    try:
                        await self._close_writer()
                        if self._process is not None:
                            await self._process.stop()
                            self._process = None
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
                        previous_python = await asyncio.to_thread(
                            self._artifact_manager.resolve_python
                        )
                        commit()
                        result = await self._reconcile_locked(
                            self._config,
                            claims_already_paused=True,
                            skip_embedding_guard=True,
                            resume_claims_on_failure=False,
                        )
                        if result.get("ok") is not True:
                            if previous_python is None:
                                # First artifact admission is durable even when
                                # its desired config cannot activate immediately.
                                # Publish wake authority only when the failed
                                # start retained a supervisor carrying that config.
                                if self._sidecar.snapshot().supervisor_can_restart:
                                    self._wake_config = deepcopy(self._config)
                                return
                            raise MemoryRuntimeActivationError("candidate runtime reconciliation failed")
                        self._wake_config = deepcopy(self._config)
                        return
                    except (Exception, asyncio.CancelledError) as activation_error:
                        try:
                            rollback()
                            if root_rollback is not None:
                                await run_blocking(root_rollback.rollback)
                            rollback_result = await self._reconcile_locked(
                                self._config,
                                claims_already_paused=True,
                                skip_embedding_guard=True,
                                resume_claims_on_failure=False,
                            )
                            if rollback_result.get("ok") is not True:
                                raise MemoryRuntimeActivationError("previous runtime reconciliation failed")
                            self._wake_config = deepcopy(self._config)
                        except Exception as rollback_error:
                            self._runtime_error = "memory_runtime_install_failed"
                            raise MemoryRuntimeActivationError("memory runtime rollback failed") from rollback_error
                        if isinstance(activation_error, asyncio.CancelledError):
                            raise
                        raise MemoryRuntimeActivationError("memory runtime activation failed") from activation_error

    async def _current_sidecar_ready(self, generation: int) -> None:
        """Accept the lifecycle module's semantic current-sidecar event."""

        async with self._reconcile_lock:
            async with self.module.lifecycle():
                snapshot = self._sidecar.snapshot()
                if not self._sidecar_ready_is_current(snapshot, generation):
                    return
                if not self._sidecar_ready_is_current(self._sidecar.snapshot(), generation):
                    return
                self._runtime_error = None
                self.module.resume_claims()
                self._resume_writer()

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
            and self._wake_config.enabled
            and not self.needs_repair
            and not self._artifact_installing
            and not self._closing
        )

    async def _processing_healthy(self) -> bool:
        """Answer processing health without waiting on a peer probe."""

        return await self._sidecar.processing_healthy()

    async def _probe_processing(self, python: Path, config: MemoryConfig) -> bool:
        """Probe a candidate configuration on a throwaway child.

        Deliberately shares no state with ``_processing_healthy``: this probe
        never touches the supervised child, and ``_reconcile_lock`` already
        serializes it. One shared lock made settings saves and capture processing
        wait on each other while holding lifecycle authority needed by reads.
        """

        return await self._sidecar.probe(python, _process_settings(config))

    def _resume_writer(self) -> None:
        self.module.resume_claims()

    async def _settle_ambiguous_provider_outcome(self, recover: bool) -> bool:
        """Prove old ownership ended and reuse the settled runtime when requested."""

        await self._sidecar.stop()
        if recover and not self._closing and not self._retired:
            self._defer_wake_until_writer_closed()
        return True

    async def _restore_provisional_claims(
        self,
        previous_config: MemoryConfig,
    ) -> bool:
        """Reactivate the last settled authority after a pre-cutover failure."""

        result = await self._reconcile_locked(
            previous_config,
            claims_already_paused=True,
            skip_embedding_guard=True,
            resume_claims_on_failure=False,
        )
        return result.get("ok") is True

    async def _close_writer(self) -> None:
        await self.module.close_writer()

    def _data_exists(self) -> bool:
        """Return a conservative status projection of vector-bearing state."""

        try:
            return self._provider_data_exists_strict()
        except Exception:
            return True

    def _provider_data_exists_strict(self) -> bool:
        """Inspect all vector-bearing state, raising when it cannot be proven empty."""

        root_has_data = self._provider_root_owner.has_data()
        return bool(root_has_data or self._store.has_provider_data_history())

    async def _embedding_change_is_admissible(
        self,
        current: MemoryConfig,
        candidate: MemoryConfig,
    ) -> bool:
        """Require a freshly proven-empty vector surface for an embedding change."""

        if not _embedding_configuration_changed(current, candidate):
            return True
        try:
            return not await asyncio.to_thread(self._provider_data_exists_strict)
        except Exception:
            # An indeterminate root/queue state cannot safely accept an
            # embedding change because it could mix vector spaces.
            return False

def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _local_data_failure_requires_repair(error: BaseException) -> bool:
    """Classify only unusable local mutable data as destructive-repair eligible."""

    chain = _exception_chain(error)
    environmental = {
        errno.EACCES,
        errno.EPERM,
        errno.ENOSPC,
        errno.EDQUOT,
        errno.EROFS,
        errno.EIO,
    }
    if any(
        isinstance(item, PermissionError)
        or (isinstance(item, OSError) and item.errno in environmental)
        for item in chain
    ):
        return False
    for item in chain:
        if isinstance(item, ProviderRootError):
            detail = str(item).lower()
            return any(
                marker in detail
                for marker in (
                    "incompatible",
                    "does not match",
                    "not empty",
                    "sentinel is unsafe",
                    "sentinel is invalid",
                    "metadata is invalid",
                )
            )
    return False


def _degraded_runtime_reason(
    error: BaseException,
    *,
    default: str = "memory_sidecar_unavailable",
) -> str:
    chain = _exception_chain(error)
    if any(isinstance(item, PermissionError) for item in chain):
        return "memory_permission_denied"
    disk_errors = {errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO}
    if any(
        isinstance(item, OSError) and item.errno in disk_errors
        for item in chain
    ):
        return "memory_disk_unavailable"
    return default


def _provider_kwargs(config: MemoryConfig) -> dict[str, str | None]:
    processing = config.runtime_processing()
    rerank = processing.rerank
    multimodal = processing.multimodal
    return {
        "llm_base_url": processing.llm.base_url,
        "llm_model": processing.llm.model,
        "llm_api_key": processing.llm.api_key,
        "embedding_base_url": processing.embedding.base_url,
        "embedding_model": processing.embedding.model,
        "embedding_api_key": processing.embedding.api_key,
        "rerank_base_url": rerank.base_url if rerank else None,
        "rerank_model": rerank.model if rerank else None,
        "rerank_api_key": rerank.api_key if rerank else None,
        "rerank_provider": rerank.rerank_provider() if rerank else None,
        "multimodal_base_url": multimodal.base_url if multimodal else None,
        "multimodal_model": multimodal.model if multimodal else None,
        "multimodal_api_key": multimodal.api_key if multimodal else None,
    }


def _attachment_capture_status(
    config: MemoryConfig,
    source_status: object,
    health: object,
    attachment_intake_enabled: bool,
) -> Literal["ready", "not_configured", "unavailable"]:
    """Project explicit IM attachment-capture readiness from config and health."""

    if not config.effective_multimodal_available():
        return "not_configured"
    if not IM_ATTACHMENT_CAPTURE_PLATFORMS:
        return "unavailable"
    if not config.enabled:
        return "unavailable"
    if not attachment_intake_enabled:
        return "unavailable"
    if source_status != "available":
        return "unavailable"
    if not isinstance(health, dict):
        return "unavailable"
    capabilities = health.get("capabilities")
    disabled = health.get("disabled_features")
    if not isinstance(capabilities, dict) or not isinstance(disabled, list):
        return "unavailable"
    if any(feature in disabled for feature in ("multimodal_llm", "parser")):
        return "unavailable"
    if capabilities.get("multimodal_llm") is not True or capabilities.get("parser") is not True:
        return "unavailable"
    return "ready"


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

    return current.runtime_embedding_identity() != candidate.runtime_embedding_identity()


def _process_settings(
    config: MemoryConfig,
) -> EverOSProcessSettings:
    return EverOSProcessSettings(
        **_provider_kwargs(config),
    )


def _memory_processing_complete(config: MemoryConfig) -> bool:
    """Return whether both configured processing providers can be contacted."""

    processing = config.runtime_processing()
    return processing.llm.complete() and processing.embedding.complete()


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
