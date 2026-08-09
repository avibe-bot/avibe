"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import os
import stat
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, TypeVar

from config import paths
from config.v2_config import CONFIG_LOCK, MemoryConfig, V2Config
from core.memory.artifact import (
    EVEROS_VERSION,
    MemoryArtifactCandidate,
    MemoryArtifactPort,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
    get_memory_artifact_manager,
)
from core.memory.blocking import run_blocking
from core.memory.clear_journal import ClearSurface
from core.memory.everos import (
    EverOSPort,
    MemoryProviderFailure,
    ProviderHealthSnapshot,
)
from core.memory.everos_insight import MemoryInsightPaths, MemoryInsightReader
from core.memory.everos_insight.recorder import clear_call_log, maintain_call_log
from core.memory.module import MemoryModule
from core.memory.maintenance import (
    ClearRecoveryResult,
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
    EverOSProcessSettings,
    SidecarOwnership,
    sidecar_record_path,
)
from core.memory.processing_record import (
    AnomalyProjection,
    MaintenanceProjection,
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
    ProviderRootMetadata,
    ProviderRootRollback,
)
from core.memory.store import MemoryStore, is_principal_id, is_project_id
from core.memory.snapshot import MemorySnapshot
from core.memory.types import (
    MemoryFailureLogEntry,
    MemoryItems,
    MemoryResult,
    OperationFailed,
    RecallItems,
    RecallPolicy,
    RecallResult,
    memory_item_payload,
)
from core.memory.worker import ProcessingEvent


logger = logging.getLogger(__name__)


_SessionLifecycleResult = TypeVar("_SessionLifecycleResult")


ARTIFACT_ACTIVATION_TIMEOUT_SECONDS = 90.0
_CALL_LOG_RETENTION_INTERVAL_SECONDS = 6 * 60 * 60
_RECORDER_DISABLED = {"state": "disabled", "reason": None}
_RECORDER_DEGRADED = {"state": "degraded", "reason": "writer_failures"}


def _clear_recovery_payload(
    recovery: ClearRecoveryResult | None,
) -> dict[str, Any] | None:
    if recovery is None:
        return None
    return {
        "state": recovery.state,
        "operation_id": recovery.operation_id,
        "occurred_at": recovery.occurred_at,
        "error_code": recovery.error_code,
        "can_resume": recovery.can_resume,
        "can_abort": recovery.can_abort,
    }


def _clear_result_payload(result: ClearResult) -> dict[str, Any]:
    if result.status == "failed":
        return {
            "status": "failed",
            "error": result.error,
            "recovery": _clear_recovery_payload(result.recovery),
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
        "clear_recovery": _clear_recovery_payload(result.clear_recovery),
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
    }


class MemorySessionLifecycleBusyError(RuntimeError):
    """Raised when a destructive session transition cannot acquire its fence."""

    code = "memory_session_lifecycle_busy"


class _UnavailableMemoryModule:
    """Capture sink for a runtime whose store could not be opened.

    ``MemoryRuntime.module`` is only ever used to capture, so this narrow adapter
    is the whole seam — not a stand-in for ``MemoryModule``'s six methods.
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
        self._process: EverOSProcessPort | None = None
        # The controller-side port only talks to the private UDS. Credentials
        # enter an EverOSPort only inside the owned child probe/sidecar.
        self._provider = EverOSPort(self._socket_path)
        self._runtime_error: str | None = None
        self._reconcile_lock = asyncio.Lock()
        self._restart_task: asyncio.Task[dict[str, Any]] | None = None
        self._ready_activation_task: asyncio.Task[None] | None = None
        self._closing = False
        self._ready_event: EverOSProcessPort | None = None
        # Single-flight state for the drain-side processing gate. Deliberately a
        # flag and a cached verdict rather than a lock: the gate must answer
        # without waiting, see ``_processing_healthy``.
        self._processing_probe_active = False
        self._processing_probe_healthy = False
        self._worker_task: asyncio.Task[None] | None = None
        self._activation_loop: asyncio.AbstractEventLoop | None = None
        self._artifact_installing = False
        self._store: MemoryStore | None = None
        self._module: MemoryModule | None = None
        self._store_error: Exception | None = None
        self._insight_reader_override = insight_reader
        self._insight_reader: MemoryInsightReader | None = None
        self._call_log_retention_task: asyncio.Task[None] | None = None
        self._process_records_calls = False
        self._recorder_health: dict[str, str | None] = dict(_RECORDER_DISABLED)
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
            opened = store or MemoryStore()
            self._artifact_manager.set_provider_root(self._provider_root)
            module = MemoryModule(
                opened,
                self._provider,
                enabled=lambda: self._config.enabled,
                runtime_error=lambda: self._runtime_error,
                starting=lambda: bool(self._process and self._process.starting),
                provider_root=self._provider_root,
                provider_root_owner=self._provider_root_owner,
                provider_root_metadata=self._active_provider_root_metadata,
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
        if self._maintenance_open():
            module._worker.pause_claims()
        self._store_error = None
        self._configure_insight_reader(self._config)
        self._artifact_manager.set_activation_coordinator(self._coordinate_artifact_activation)
        return True

    @property
    def available(self) -> bool:
        """Whether the local store opened. False keeps every read closed."""

        return self._module is not None

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
        )

    def _processing_record_port(self) -> MemoryProcessingRecordPort:
        return MemoryProcessingRecordPort(
            observe_maintenance=self._processing_record_maintenance_observation,
            observe_health=self._processing_record_health,
            failure_log=self._processing_record_failure_log,
            recorder_health=lambda: dict(self._recorder_health),
            observe_sources=self._processing_record_sources,
            maintenance=self._processing_record_maintenance,
        )

    @asynccontextmanager
    async def _maintenance_fence(self) -> AsyncIterator[None]:
        """Acquire destructive fences in their single permitted order."""

        async with self._reconcile_lock, self.module._lifecycle_lock:
            async with self.module._root_lifecycle_lock():
                yield

    @asynccontextmanager
    async def _maintenance_boot_recovery_fence(self) -> AsyncIterator[None]:
        """Add the root fence while reconcile and module fences are already held."""

        async with self.module._root_lifecycle_lock():
            yield

    def _maintenance_runtime_state(self) -> MaintenanceRuntimeState:
        return MaintenanceRuntimeState(
            artifact_installing=self._artifact_installing,
        )

    def _enter_maintenance(self) -> None:
        if self._module is not None:
            self._module._clear_active = True

    def _leave_maintenance(self) -> None:
        if self._module is not None:
            self._module._clear_active = False

    def _resume_maintenance_claims(self) -> None:
        if self._module is not None:
            self._module._worker.resume_claims()

    def _configure_insight_reader(self, config: MemoryConfig) -> None:
        if self._insight_reader_override is not None:
            self._insight_reader = self._insight_reader_override
            return
        if self._store is None:
            self._insight_reader = None
            return
        base_urls = tuple(
            value
            for value in (
                config.processing.llm.base_url,
                config.processing.embedding.base_url,
            )
            if value
        )
        exact_redaction_values = tuple(
            value
            for value in (
                config.processing.llm.api_key,
                config.processing.embedding.api_key,
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

    async def _reap_recorded_sidecar_if_unowned(self) -> bool:
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
        retained, so the recovery stays available to the next attempt.
        """

        async with self._reconcile_lock:
            if self._process is not None:
                return False
            ownership = SidecarOwnership(
                record_path=sidecar_record_path(self._memory_dir),
                socket_path=self._socket_path,
                provider_root=self._provider_root,
            )
            try:
                await ownership.reap()
            except Exception as exc:
                logger.warning("Recorded EverOS sidecar recovery did not finish: %s", exc)
                return False
            return True

    async def reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Apply persisted config without restarting the Avibe service."""

        try:
            return await self._reconcile(config)
        finally:
            if self._maintenance is not None:
                self._maintenance.ensure_housekeeping()

    async def _reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Run one reconciliation owned by the public lifecycle operation."""

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
            self._activation_loop = asyncio.get_running_loop()
            if self._artifact_installing:
                return {"ok": False, "error": "memory_runtime_install_failed"}
            # This is deliberately the same lifecycle lock Clear uses. A settings
            # save cannot race a root wipe or replace sidecar credentials halfway
            # through an active provider call.
            async with self.module._lifecycle_lock:
                restore_operation_open = (
                    self._maintenance is not None
                    and self._maintenance.has_open_restore()
                )
                maintenance_open = self._maintenance_open()
                if (
                    maintenance_open
                    and self._can_disable_without_maintenance_authority(config)
                ):
                    result = await self._disable_locked(config)
                elif restore_operation_open and not recorded_sidecar_reaped:
                    self.module._worker.pause_claims()
                    return {"ok": False, "error": "memory_clear_failed"}
                elif maintenance_open and not restore_operation_open:
                    self.module._worker.pause_claims()
                    return {"ok": False, "error": "memory_clear_failed"}
                else:
                    result = await self._reconcile_locked(config)
                if result.get("ok") is True:
                    self._restart_config = deepcopy(config)
                return result

    async def _disable_locked(self, config: MemoryConfig) -> dict[str, Any]:
        """Stop every active Memory component without consulting maintenance state."""

        self.module._worker.pause_claims()
        self._config = config
        self._configure_insight_reader(config)
        self._provider = EverOSPort(self._socket_path)
        self.module._replace_provider(self._provider)
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

        if self._maintenance is not None and self._maintenance.has_open_restore():
            recovered = await self._maintenance.recover_boot()
            if not recovered:
                self.module._worker.pause_claims()
                self._runtime_error = "memory_clear_failed"
                return {"ok": False, "error": self._runtime_error}

        if self._maintenance_open():
            self.module._worker.pause_claims()
            if self._can_disable_without_maintenance_authority(config):
                return await self._disable_locked(config)
            return {"ok": False, "error": "memory_clear_failed"}

        embedding_changed = not skip_embedding_guard and (
            config.embedding_change_pending or _embedding_configuration_changed(self._config, config)
        )
        claims_paused = claims_already_paused
        if embedding_changed:
            # Stop the worker before inspecting provider state. A capture may
            # still enqueue while settings are being reconciled, but no
            # old-embedding drain can cross this boundary.
            if not await self.module._worker.pause_and_wait():
                # ``pause_and_wait`` fences claims before waiting, so a timeout
                # leaves them fenced. Releasing here is what keeps a failed
                # settings save from silently stopping the drain loop forever.
                if resume_claims_on_failure:
                    self.module._worker.resume_claims()
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
                    self.module._worker.resume_claims()
                    claims_paused = False

        if config.embedding_change_pending:
            # A durable candidate marker prevents a post-save crash from
            # comparing the candidate against itself on next startup. Clear it
            # only after the guarded inspection succeeds, while claims remain
            # paused, so no capture can resume against an unverified config.
            if not await asyncio.to_thread(self._settle_embedding_change_pending, config):
                error = "memory_runtime_install_failed"
                if not (self._process and self._process.running):
                    self._runtime_error = error
                if claims_paused and resume_claims_on_failure:
                    self.module._worker.resume_claims()
                return {"ok": False, "error": error}
            # Settlement is independently durable even when later candidate
            # activation fails. Mirror only that fact into the replay snapshot.
            self._restart_config.embedding_change_pending = False

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
                self.module._worker.resume_claims()
            return {"ok": False, "error": error}
        if not await self._probe_processing(python, config):
            error = "memory_processing_failed"
            if not (self._process and self._process.running):
                self._runtime_error = error
            if claims_paused and resume_claims_on_failure:
                self.module._worker.resume_claims()
            return {"ok": False, "error": error}

        # Every enabled reconciliation receives a fresh process. Endpoint,
        # model, and key changes belong exclusively in its allowlisted child
        # environment and must never leave an old sidecar running.
        if not claims_paused and not await self.module._worker.pause_and_wait():
            # Same fence release as the embedding guard above: a timed-out pause
            # must not leave the worker permanently unable to claim.
            if resume_claims_on_failure:
                self.module._worker.resume_claims()
            self._runtime_error = "memory_clear_failed"
            return {"ok": False, "error": self._runtime_error}
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False

        self._config = config
        self._configure_insight_reader(config)
        self._provider = candidate_provider
        self.module._replace_provider(self._provider)
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
                self.module._worker.resume_claims()
            return {"ok": False, "error": self._runtime_error}

        await self._stop_call_log_retention()
        sidecar: EverOSProcessPort | None = None

        async def before_recorder_start() -> None:
            if sidecar is not self._process:
                raise RuntimeError("stale EverOS recorder supervisor")
            await self._stop_call_log_retention()

        async def recorder_reaped() -> None:
            if sidecar is not self._process:
                return
            self._process_records_calls = False
            self._ensure_call_log_retention()

        def sidecar_ready() -> None:
            if sidecar is not None:
                self._schedule_sidecar_ready(sidecar)

        settings = _process_settings(
            config,
            call_log_db_path=self._call_log_db_path,
        )
        sidecar = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
            on_ready=sidecar_ready,
            before_start=before_recorder_start,
            on_reaped=recorder_reaped,
        )
        self._process = sidecar
        self._process_records_calls = True
        try:
            started = await self._process.start()
        except BaseException:
            self._process_records_calls = False
            self._ensure_call_log_retention()
            raise
        if not started:
            self._process_records_calls = False
            self._ensure_call_log_retention()
            self._runtime_error = "memory_sidecar_unavailable"
            return {"ok": False, "error": self._runtime_error}
        if self._ready_event is sidecar:
            self._ready_event = None
        self._update_recorder_health(_RECORDER_DEGRADED)
        self._runtime_error = None
        self.module._worker.resume_claims()
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
                clear_recovery=None,
                can_clear=False,
            )
        return await maintenance.observe(operator_ref=operator_ref)

    async def _processing_record_health(
        self,
        maintenance_reason: str | None,
    ) -> RuntimeHealthObservation:
        snapshot: ProviderHealthSnapshot | None = None
        reason: str | None = None
        if not self._config.enabled:
            return RuntimeHealthObservation(
                snapshot=None,
                unavailable_reason="memory_disabled",
            )
        if not self.available:
            return RuntimeHealthObservation(
                snapshot=None,
                unavailable_reason="memory_sidecar_unavailable",
            )
        if maintenance_reason is not None:
            return RuntimeHealthObservation(
                snapshot=None,
                unavailable_reason=maintenance_reason,
            )
        async with self._reconcile_lock:
            reason = self._processing_record_state_reason(maintenance_reason)
            process = self._process
        if reason is not None:
            return RuntimeHealthObservation(snapshot=None, unavailable_reason=reason)
        try:
            snapshot = await self._provider.health_snapshot()
        except MemoryProviderFailure as failure:
            reason = failure.error
        except Exception:
            reason = "memory_sidecar_unavailable"
        async with self._reconcile_lock:
            current_reason = self._processing_record_state_reason(maintenance_reason)
            if self._process is not process or current_reason is not None:
                return RuntimeHealthObservation(
                    snapshot=None,
                    unavailable_reason=current_reason or "memory_sidecar_unavailable",
                )
            if snapshot is not None:
                self._update_recorder_health(snapshot.recorder)
        return RuntimeHealthObservation(snapshot=snapshot, unavailable_reason=reason)

    def _processing_record_state_reason(
        self,
        maintenance_reason: str | None,
    ) -> str | None:
        if not self._config.enabled:
            return "memory_disabled"
        if not self.available:
            return "memory_sidecar_unavailable"
        if maintenance_reason is not None:
            return maintenance_reason
        if self._process is None or not self._process.running:
            return self._runtime_error or "memory_sidecar_unavailable"
        return None

    async def _processing_record_failure_log(
        self,
        maintenance_reason: str | None,
    ) -> tuple[MemoryFailureLogEntry, ...]:
        if not self.available:
            raise self._unavailable()
        if maintenance_reason is not None or self.module._clear_active:
            return ()
        async with self.module._root_lifecycle_lock():
            if self.module._clear_active:
                return ()
            return await run_blocking(self._store.failure_log, limit=50)

    async def _processing_record_sources(
        self,
        maintenance_reason: str | None,
    ) -> ProcessingSourceObservations:
        if maintenance_reason is not None:
            unavailable = SourceObservation(
                "unavailable",
                reason=maintenance_reason,
            )
            return ProcessingSourceObservations(
                everos=unavailable,
                capture=unavailable,
                calls=unavailable,
            )
        reader = self._insight_reader
        if not self.available or reader is None:
            raise self._unavailable()
        return await self._run_insight_read(reader.source_observation)

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
        self._processing_record.observe_recorder(
            self._recorder_health,
            observed_at=observed_at,
        )

    async def processing_record_payload(
        self,
        *,
        operator_ref: str | None = None,
    ) -> dict[str, Any]:
        summary = await self._processing_record.read(operator_ref)
        return _processing_record_payload(summary)

    async def status_payload(self) -> dict[str, Any]:
        runtime = await self._processing_record.read_runtime()
        return _runtime_health_payload(runtime)

    async def failure_log_payload(
        self,
        *,
        operator_ref: str | None = None,
    ) -> dict[str, Any]:
        anomalies, maintenance = await self._processing_record.read_failures(
            operator_ref
        )
        if anomalies.source.status == "unavailable":
            raise self._unavailable()
        return {
            "status": "ok",
            "items": [asdict(entry) for entry in anomalies.items],
            "recovery": _clear_recovery_payload(maintenance.clear_recovery),
        }

    async def maintenance_payload(
        self,
        *,
        operator_ref: str | None = None,
    ) -> dict[str, Any]:
        """Return cheap local maintenance facts without probing or scanning EverOS."""

        result = await self._processing_record.read_maintenance(operator_ref)
        return {
            "status": "ok",
            "data_exists": result.data_exists,
            "can_clear": result.can_clear,
            "clear_recovery": _clear_recovery_payload(result.clear_recovery),
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

        if not self.available or not self._config.enabled or self._maintenance_open():
            return False
        timeout = _final_flush_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        admission_lock = self.module._capture_admission_lock(
            principal_id=principal_id,
            project_id=project_id,
            session_id=raw_session_id,
        )
        acquired = False
        try:
            await asyncio.wait_for(admission_lock.acquire(), timeout=timeout)
            acquired = True
            return await self._final_flush_under_admission(
                principal_id=principal_id,
                project_id=project_id,
                raw_session_id=raw_session_id,
                deadline=deadline,
            )
        except asyncio.TimeoutError:
            return False
        finally:
            if acquired:
                admission_lock.release()

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

        if not self.available or not self._config.enabled or self._maintenance_open():
            return await operation()
        timeout = _final_flush_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        admission_lock = self.module._capture_admission_lock(
            principal_id=principal_id,
            project_id=project_id,
            session_id=raw_session_id,
        )
        try:
            await asyncio.wait_for(admission_lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise MemorySessionLifecycleBusyError(
                "memory capture admission did not quiesce before the deadline"
            ) from error

        try:
            await self._final_flush_under_admission(
                principal_id=principal_id,
                project_id=project_id,
                raw_session_id=raw_session_id,
                deadline=deadline,
            )
            return await operation()
        finally:
            admission_lock.release()

    async def run_session_scopes_lifecycle(
        self,
        *,
        scopes: tuple[tuple[str, str], ...],
        raw_session_id: str,
        operation: Callable[[], Awaitable[_SessionLifecycleResult]],
        deadline_seconds: float = 5.0,
    ) -> _SessionLifecycleResult:
        """Flush all session scopes and run one transition under every fence."""

        canonical_scopes = tuple(sorted(set(scopes)))
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
        if not self.available or not self._config.enabled:
            return await operation()
        if self._maintenance_open():
            raise MemorySessionLifecycleBusyError("memory session lifecycle is unavailable")

        timeout = _final_flush_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        locks = [
            self.module._capture_admission_lock(
                principal_id=principal_id,
                project_id=project_id,
                session_id=raw_session_id,
            )
            for principal_id, project_id in canonical_scopes
        ]
        acquired: list[asyncio.Lock] = []
        try:
            for admission_lock in locks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                await asyncio.wait_for(admission_lock.acquire(), timeout=remaining)
                acquired.append(admission_lock)
        except asyncio.TimeoutError as error:
            for admission_lock in reversed(acquired):
                admission_lock.release()
            raise MemorySessionLifecycleBusyError(
                "memory capture admission did not quiesce before the deadline"
            ) from error
        except asyncio.CancelledError:
            for admission_lock in reversed(acquired):
                admission_lock.release()
            raise

        try:
            for principal_id, project_id in canonical_scopes:
                await self._final_flush_under_admission(
                    principal_id=principal_id,
                    project_id=project_id,
                    raw_session_id=raw_session_id,
                    deadline=deadline,
                )
            return await operation()
        finally:
            for admission_lock in reversed(acquired):
                admission_lock.release()

    async def _final_flush_under_admission(
        self,
        *,
        principal_id: str,
        project_id: str,
        raw_session_id: str,
        deadline: float,
    ) -> bool:
        """Flush one session while its capture admission lock is already held."""

        if not self.available or not self._config.enabled or self._maintenance_open():
            return False
        try:
            session_ref = await asyncio.to_thread(
                self._store.provider_session_ref,
                principal_id=principal_id,
                project_ref=project_id,
                session_id=raw_session_id,
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            return await self.module._worker.coordinator.final_flush(
                session_ref,
                deadline_seconds=remaining,
            )
        except asyncio.TimeoutError:
            return False
        except (TypeError, ValueError):
            return False
        except Exception:
            logger.warning("Memory final flush failed")
            return False

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
        return _result_payload(
            await self.module.recall(
                query,
                policy=policy,
                principal_id=principal_id,
                project_id=project_id,
                current_session_id=current_session_id,
            )
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
        async with self.module._lifecycle_lock:
            return await run_blocking(operation)

    async def create_backup(self, backup_id: str | None = None) -> MemorySnapshot:
        """Create one ordinary Memory backup under the full maintenance fence."""

        if not self.available:
            raise self._unavailable()
        return await self._require_maintenance().create_backup(backup_id)

    async def restore_backup(
        self,
        backup_id: str,
        *,
        expected_manifest_sha256: str,
        expected_surface_digests: Mapping[str, str | None],
    ) -> MemorySnapshot:
        """Restore one verified ordinary Memory backup under the same fence."""

        if not self.available:
            raise self._unavailable()
        return await self._require_maintenance().restore_backup(
            backup_id,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_surface_digests=expected_surface_digests,
        )

    async def clear(self, *, operator_ref: str) -> dict[str, Any]:
        if not self.available:
            raise self._unavailable()
        result = await self._require_maintenance().clear(operator_ref=operator_ref)
        return _clear_result_payload(result)

    async def resume_clear(
        self,
        operation_id: str,
        *,
        operator_ref: str,
    ) -> dict[str, Any]:
        if not self.available:
            raise self._unavailable()
        result = await self._require_maintenance().resume_clear(
            operation_id,
            operator_ref=operator_ref,
        )
        return _clear_result_payload(result)

    async def abort_clear(
        self,
        operation_id: str,
        *,
        operator_ref: str,
    ) -> dict[str, Any]:
        if not self.available:
            raise self._unavailable()
        result = await self._require_maintenance().abort_clear(
            operation_id,
            operator_ref=operator_ref,
        )
        return _clear_result_payload(result)

    async def _pause_clear_claims(self) -> None:
        if not await self.module._worker.pause_and_wait(
            timeout_seconds=self.module._clear_drain_timeout_seconds
        ):
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
            self._set_recorder_health_disabled()
            return
        if surface.surface == "attachments":
            await run_blocking(self.module._attachment_store.clear_all)
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
            self.module._worker.resume_claims()
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

        self._activation_loop = asyncio.get_running_loop()
        async with self._reconcile_lock:
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
                async with self.module._lifecycle_lock:
                    try:
                        claims_paused = await self.module._worker.pause_and_wait()
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

        task = self._restart_task
        if task is None or task.done():
            task = asyncio.create_task(self._restart_once(), name="memory-restart")
            self._restart_task = task

            def clear_restart(completed: asyncio.Task[dict[str, Any]]) -> None:
                if self._restart_task is completed:
                    self._restart_task = None

            task.add_done_callback(clear_restart)
        return await asyncio.shield(task)

    async def _restart_once(self) -> dict[str, Any]:
        try:
            async with self._reconcile_lock:
                return await self._restart_locked()
        except Exception:
            logger.exception("Memory sidecar restart failed")
            return {"ok": False, "error": "memory_restart_failed"}
        finally:
            if self._maintenance is not None:
                self._maintenance.ensure_housekeeping()

    async def _restart_locked(self) -> dict[str, Any]:
        """Replace the sidecar while ``_reconcile_lock`` is held."""

        self._activation_loop = asyncio.get_running_loop()
        if not self.available or self._store is None or self._module is None:
            return {"ok": False, "error": "memory_store_unavailable"}
        if self._artifact_installing:
            return {"ok": False, "error": "memory_restart_failed"}

        replay = deepcopy(self._restart_config)
        if not replay.enabled:
            return {"ok": False, "error": "memory_disabled"}
        if replay.embedding_change_pending:
            return {"ok": False, "error": "memory_clear_failed"}

        python = await asyncio.to_thread(self._artifact_manager.resolve_python)
        if python is None:
            return {"ok": False, "error": "memory_restart_failed"}

        async with self.module._lifecycle_lock:
            if self._maintenance_open():
                self.module._worker.pause_claims()
                return {"ok": False, "error": "memory_clear_failed"}

            # Recovery may resume claims. Reinstate the fence synchronously,
            # before a drain task can run another claim.
            worker = self.module._worker
            worker.pause_claims()
            old_process = self._process
            try:
                # The grace budget applies only to the current drain tick. A
                # timeout leaves the current process/provider ownership intact.
                if not await worker.pause_and_wait(timeout_seconds=5.0):
                    if old_process is not None and old_process.running:
                        worker.resume_claims()
                        self._ensure_worker()
                    return {"ok": False, "error": "memory_restart_failed"}
                await self._stop_worker()
            except Exception:
                if old_process is not None and old_process.running:
                    worker.resume_claims()
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
            self.module._replace_provider(self._provider)
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

            await self._stop_call_log_retention()
            sidecar: EverOSProcessPort | None = None

            async def before_recorder_start() -> None:
                if sidecar is not self._process:
                    raise RuntimeError("stale EverOS recorder supervisor")
                await self._stop_call_log_retention()

            async def recorder_reaped() -> None:
                if sidecar is not self._process:
                    return
                self._process_records_calls = False
                self._ensure_call_log_retention()

            def sidecar_ready() -> None:
                if sidecar is not None:
                    self._schedule_sidecar_ready(sidecar)

            sidecar = self._process_factory(
                python,
                provider_root=self._provider_root,
                effective_home=self._effective_home,
                settings=_process_settings(
                    self._config,
                    call_log_db_path=self._call_log_db_path,
                ),
                socket_path=self._socket_path,
                on_ready=sidecar_ready,
                before_start=before_recorder_start,
                on_reaped=recorder_reaped,
            )
            self._process = sidecar
            self._process_records_calls = True
            worker.begin_new_lease_activation()
            try:
                started = await sidecar.start()
            except Exception:
                self._process_records_calls = False
                self._ensure_call_log_retention()
                self._runtime_error = "memory_restart_failed"
                return {"ok": False, "error": self._runtime_error}
            if not started:
                self._process_records_calls = False
                self._ensure_call_log_retention()
                self._runtime_error = "memory_sidecar_unavailable"
                return {"ok": False, "error": self._runtime_error}
            if self._ready_event is sidecar:
                self._ready_event = None

            self._update_recorder_health(_RECORDER_DEGRADED)
            self._runtime_error = None
            worker.resume_claims()
            self._ensure_worker()
            return {"ok": True, "state": "ready"}

    async def close(self) -> None:
        self._closing = True
        if self._maintenance is not None:
            await self._maintenance.close()
        restart_task = self._restart_task
        if restart_task is not None and restart_task is not asyncio.current_task():
            try:
                await asyncio.shield(restart_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        # Ready callbacks are synchronous schedulers. Close their admission
        # before the first shutdown await so a late supervisor notification
        # cannot create a new drain task behind this close operation.
        self._activation_loop = None
        self._ready_event = None
        ready_task = self._ready_activation_task
        if ready_task is not None and ready_task is not asyncio.current_task():
            try:
                await asyncio.shield(ready_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if self.available:
            try:
                await self.module._worker.prepare_shutdown()
            finally:
                await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False
        await self._stop_call_log_retention()
        self._artifact_manager.set_activation_coordinator(None)

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
        root_state: MemoryProviderRootState,
        commit: Callable[[], None],
        rollback: Callable[[], None],
    ) -> None:
        """Bridge the synchronous shared installer into the controller loop."""

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
            async with self.module._lifecycle_lock:
                if self._maintenance_open():
                    self.module._worker.pause_claims()
                    raise MemoryRuntimeActivationError("memory clear recovery is required")
                if not await self.module._worker.pause_and_wait():
                    self.module._worker.resume_claims()
                    raise MemoryRuntimeActivationError("memory worker could not pause")
                async with self.module._root_lifecycle_lock():
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
                            skip_embedding_guard=not self._config.embedding_change_pending,
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
                                skip_embedding_guard=not self._config.embedding_change_pending,
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

    def _schedule_sidecar_ready(self, process: EverOSProcessPort) -> None:
        """Coalesce a supervisor notification into Runtime-owned lock work."""

        if self._activation_loop is None or process is not self._process:
            return
        self._ready_event = process
        task = self._ready_activation_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self._activate_ready_events(),
            name="memory-ready-activation",
        )
        self._ready_activation_task = task

        def clear_activation(completed: asyncio.Task[None]) -> None:
            if self._ready_activation_task is completed:
                self._ready_activation_task = None

        task.add_done_callback(clear_activation)

    async def _activate_ready_events(self) -> None:
        while True:
            async with self._reconcile_lock:
                async with self.module._lifecycle_lock:
                    process = self._ready_event
                    self._ready_event = None
                    if process is None:
                        return
                    if not self._ready_event_is_current(process):
                        continue
                    if self._maintenance_open():
                        self.module._worker.pause_claims()
                        continue
                    if not self._ready_event_is_current(process):
                        continue
                    self._process_records_calls = True
                    await self._stop_call_log_retention()
                    if not self._ready_event_is_current(process):
                        continue
                    self._runtime_error = None
                    self.module._worker.resume_claims()
                    self._ensure_worker()

    def _ready_event_is_current(
        self,
        process: EverOSProcessPort,
    ) -> bool:
        return bool(
            self._activation_loop is not None
            and process is self._process
            and process.running
            and self._config.enabled
            and not self._config.embedding_change_pending
            and self._restart_config.enabled
            and not self._restart_config.embedding_change_pending
            and not self._artifact_installing
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

        if self._processing_probe_active:
            return self._processing_probe_healthy
        self._processing_probe_active = True
        try:
            process = self._process
            healthy = bool(process is not None and await process.processing_healthy())
        finally:
            self._processing_probe_active = False
        self._processing_probe_healthy = healthy
        return healthy

    async def _probe_processing(self, python: Path, config: MemoryConfig) -> bool:
        """Probe a candidate configuration on a throwaway child.

        Deliberately shares no state with ``_processing_healthy``: this probe
        never touches the supervised child, and ``_reconcile_lock`` already
        serializes it. One shared lock made a settings save and the drain loop
        wait on each other -- the drain past its lifecycle fences, and the
        reconcile without any bound at all while holding the module lifecycle
        lock every read and Clear needs.
        """

        probe_process = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=_process_settings(config),
            socket_path=self._socket_path,
        )
        return await probe_process.processing_healthy()

    def _ensure_worker(self) -> None:
        if self._maintenance_open():
            self.module._worker.pause_claims()
            return
        if self._worker_task is None or self._worker_task.done():
            self.module._worker.begin_activation()
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
        task = self._call_log_retention_task
        if (
            self._process_records_calls
            or self._recorder_health.get("reason") == "call_log_corrupt"
            or not self._call_log_exists()
        ):
            return
        if task is not None and not task.done():
            return
        self._call_log_retention_task = asyncio.create_task(
            self._call_log_retention_loop(),
            name="memory-call-log-retention",
        )

    async def _stop_call_log_retention(self) -> None:
        task = self._call_log_retention_task
        self._call_log_retention_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _call_log_retention_loop(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                should_continue, reason = await self._maintain_call_log_once()
                if not should_continue:
                    return
                if reason is not None:
                    self._update_recorder_health(
                        {"state": "degraded", "reason": reason}
                    )
                    if reason == "call_log_corrupt":
                        return
                elif self._recorder_health.get("reason") != "call_log_corrupt":
                    self._set_recorder_health_disabled()
                await asyncio.sleep(_CALL_LOG_RETENTION_INTERVAL_SECONDS)
        finally:
            if self._call_log_retention_task is current:
                self._call_log_retention_task = None

    async def _maintain_call_log_once(self) -> tuple[bool, str | None]:
        async with self._reconcile_lock:
            if self._process_records_calls or not self._call_log_exists():
                return False, None
            if self._module is None:
                return True, await self._run_call_log_maintenance()
            async with self.module._lifecycle_lock:
                if self._process_records_calls or not self._call_log_exists():
                    return False, None
                return True, await self._run_call_log_maintenance()

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
                await self.module._worker.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Memory drain activation failed; retrying recovery")
                await self._recover_failed_drain()
            await asyncio.sleep(1.0)

    async def _recover_failed_drain(self) -> None:
        """Quiesce detached session work before rotating the worker lease."""

        worker = self.module._worker
        while self._config.enabled:
            try:
                if await worker.pause_and_wait():
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
                    if await worker.pause_and_wait():
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Memory drain quiescence failed; retrying")
                await asyncio.sleep(1.0)
            else:
                return

            worker.begin_new_lease_activation()
            while self._config.enabled:
                try:
                    # Claims remain paused, so this pass can only run boot recovery.
                    await worker.drain()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Memory drain activation failed; retrying recovery")
                    await asyncio.sleep(1.0)
                    continue
                if not self._maintenance_open():
                    worker.resume_claims()
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

    def _settle_embedding_change_pending(self, config: MemoryConfig) -> bool:
        """Clear a persisted candidate marker only when its full config still matches."""

        try:
            with CONFIG_LOCK:
                persisted = V2Config.load()
                if not _same_memory_configuration(persisted.memory, config):
                    return False
                if persisted.memory.embedding_change_pending:
                    persisted.memory.embedding_change_pending = False
                    persisted.save()
                config.embedding_change_pending = False
            return True
        except Exception:
            return False


def _provider_kwargs(config: MemoryConfig) -> dict[str, str | None]:
    return {
        "llm_base_url": config.processing.llm.base_url,
        "llm_model": config.processing.llm.model,
        "llm_api_key": config.processing.llm.api_key,
        "embedding_base_url": config.processing.embedding.base_url,
        "embedding_model": config.processing.embedding.model,
        "embedding_api_key": config.processing.embedding.api_key,
    }


def _final_flush_timeout(value: float) -> float:
    try:
        return max(float(value), 0.001)
    except (TypeError, ValueError):
        return 0.001


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


_SETTLEMENT_IRRELEVANT_FIELDS = {
    # The marker this comparison exists to settle.
    "embedding_change_pending": False,
}


def _same_memory_configuration(current: MemoryConfig, candidate: MemoryConfig) -> bool:
    """Compare persisted candidates while ignoring settlement-irrelevant fields."""

    return (
        replace(current, **_SETTLEMENT_IRRELEVANT_FIELDS)
        == replace(candidate, **_SETTLEMENT_IRRELEVANT_FIELDS)
    )


def _process_settings(
    config: MemoryConfig,
    *,
    call_log_db_path: Path | None = None,
) -> EverOSProcessSettings:
    return EverOSProcessSettings(
        **_provider_kwargs(config),
        call_log_db_path=call_log_db_path,
    )


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
