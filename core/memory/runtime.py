"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future as ThreadFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, replace
from collections.abc import Callable
from pathlib import Path
from typing import Any

from config import paths
from config.v2_config import CONFIG_LOCK, MemoryConfig, V2Config
from core.memory.artifact import (
    EVEROS_VERSION,
    MemoryArtifactCandidate,
    MemoryArtifactPort,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
    PROVIDER_ROOT_CONTROL_FILES,
    get_memory_artifact_manager,
)
from core.memory.everos import EverOSPort
from core.memory.everos_insight import MemoryInsightPaths, MemoryInsightReader
from core.memory.everos_insight.recorder import clear_call_log, maintain_call_log
from core.memory.module import MemoryModule
from core.memory.process import (
    EverOSProcess,
    EverOSProcessFactory,
    EverOSProcessPort,
    EverOSProcessSettings,
    SidecarOwnership,
    sidecar_record_path,
)
from core.memory.store import MemoryStore, TERMINAL_TOMBSTONE_RETENTION
from core.memory.types import (
    ClearCompleted,
    MemoryItems,
    MemoryResult,
    MemoryStatus,
    OperationFailed,
    memory_item_payload,
)
from core.memory.worker import ProcessingEvent


logger = logging.getLogger(__name__)


ARTIFACT_ACTIVATION_TIMEOUT_SECONDS = 90.0
_CALL_LOG_RETENTION_INTERVAL_SECONDS = 6 * 60 * 60
_RECORDER_DISABLED = {"state": "disabled", "reason": None}
_RECORDER_DEGRADED = {"state": "degraded", "reason": "writer_failures"}


class MemoryStoreUnavailableError(RuntimeError):
    """Raised when the controller cannot safely open the local Memory store."""


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
        self._effective_home = effective_home or paths.get_vibe_remote_dir()
        self._artifact_manager: MemoryArtifactPort = artifact_manager or get_memory_artifact_manager()
        self._process_factory: EverOSProcessFactory = process_factory or EverOSProcess
        self._processing_event = processing_event
        self._process: EverOSProcessPort | None = None
        # The controller-side port only talks to the private UDS. Credentials
        # enter an EverOSPort only inside the owned child probe/sidecar.
        self._provider = EverOSPort(self._socket_path)
        self._runtime_error: str | None = None
        self._reconcile_lock = asyncio.Lock()
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
                clear_provider_data=self._stop_sidecar_for_clear,
                provider_root_format=self._artifact_manager.provider_root_format()
                or f"everos-{EVEROS_VERSION}",
                artifact_fingerprint=self._artifact_manager.artifact_fingerprint() or "memory-runtime-unavailable",
                compatible_provider_root_formats=_active_compatible_root_formats(self._artifact_manager),
                processing_event=self._processing_event,
            )
        except Exception as exc:
            self._store_error = exc
            logger.exception("Memory store initialization failed; continuing with Memory unavailable")
            return False
        self._store = opened
        self._module = module
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
        self._insight_reader = MemoryInsightReader(
            MemoryInsightPaths(
                self._provider_root,
                self._store.path,
                self._call_log_db_path,
            ),
            provider_base_urls=base_urls,
        )

    async def _reap_recorded_sidecar_if_unowned(self) -> None:
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
                return
            ownership = SidecarOwnership(
                record_path=sidecar_record_path(self._memory_dir),
                socket_path=self._socket_path,
                provider_root=self._provider_root,
            )
            try:
                await ownership.reap()
            except Exception as exc:
                logger.warning("Recorded EverOS sidecar recovery did not finish: %s", exc)

    async def reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Apply persisted config without restarting the Avibe service."""

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
                self._ensure_call_log_retention()
                return {"ok": True, "state": "disabled"}
            if not self._open_store():
                logger.warning("Memory store remains unavailable during reconciliation")
                return {"ok": False, "error": "memory_store_unavailable"}
        async with self._reconcile_lock:
            self._activation_loop = asyncio.get_running_loop()
            if self._artifact_installing:
                return {"ok": False, "error": "memory_runtime_install_failed"}
            # A durable clear marker always wins over sidecar startup. Recovery
            # owns the same worker/root lifecycle and must finish before a new
            # child can create or read provider state.
            recovery = await self.module._recover_interrupted_clear()
            if isinstance(recovery, OperationFailed):
                self._runtime_error = recovery.error
                return {"ok": False, "error": recovery.error}
            # This is deliberately the same lifecycle lock Clear uses. A settings
            # save cannot race a root wipe or replace sidecar credentials halfway
            # through an active provider call.
            async with self.module._lifecycle_lock:
                return await self._reconcile_locked(config)

    async def _reconcile_locked(
        self,
        config: MemoryConfig,
        *,
        claims_already_paused: bool = False,
        skip_embedding_guard: bool = False,
        resume_claims_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Reconcile while both controller and module lifecycle locks are held."""

        capture_revoked = (
            self._config.diagnostics.log_provider_calls
            and not config.diagnostics.log_provider_calls
        )
        if capture_revoked:
            # Revocation precedes every candidate probe. A failed endpoint or
            # artifact replacement may retain the old functional settings, but
            # it must never retain a child that can append diagnostic payloads.
            await self._stop_worker()
            if self._process is not None:
                await self._process.stop()
                self._process = None
            self._process_records_calls = False
            self._config = replace(self._config, diagnostics=config.diagnostics)
            self._configure_insight_reader(self._config)
            self._reset_recorder_health_unless_corrupt()
            self._ensure_call_log_retention()

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

        if not config.enabled:
            self._config = config
            self._configure_insight_reader(config)
            self._provider = EverOSPort(self._socket_path)
            self.module._replace_provider(self._provider)
            await self._stop_worker()
            if self._process is not None:
                await self._process.stop()
                self._process = None
            self._process_records_calls = False
            self._reset_recorder_health_unless_corrupt()
            self._ensure_call_log_retention()
            self._runtime_error = None
            if claims_paused:
                self.module._worker.resume_claims()
            return {"ok": True, "state": "disabled"}

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
        await self._apply_active_artifact_metadata()
        try:
            meta = await asyncio.to_thread(self._store.ensure_meta)
            await asyncio.to_thread(self.module._ensure_owned_provider_root, meta)
        except Exception:
            self._runtime_error = "memory_clear_failed"
            if resume_claims_on_failure:
                self.module._worker.resume_claims()
            return {"ok": False, "error": self._runtime_error}

        records_calls = config.diagnostics.log_provider_calls
        if records_calls:
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

        settings = _process_settings(
            config,
            call_log_db_path=self._call_log_db_path if records_calls else None,
        )
        sidecar = self._process_factory(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            settings=settings,
            socket_path=self._socket_path,
            on_ready=self._on_sidecar_ready,
            before_start=before_recorder_start if records_calls else None,
            on_reaped=recorder_reaped if records_calls else None,
        )
        self._process = sidecar
        self._process_records_calls = records_calls
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
        if records_calls:
            self._recorder_health = dict(_RECORDER_DEGRADED)
        else:
            self._reset_recorder_health_unless_corrupt()
            self._ensure_call_log_retention()
        self._runtime_error = None
        self.module._worker.resume_claims()
        self._ensure_worker()
        return {"ok": True, "state": "ready"}

    async def status_payload(self) -> dict[str, Any]:
        if not self.available:
            return {
                **asdict(MemoryStatus(state="error", error="memory_store_unavailable")),
                # Unknown store contents must keep embedding changes fail-closed.
                "data_exists": True,
                "recorder": await self._recorder_status_payload(),
            }
        status = await self.module.status()
        # No ``profile_warning`` here: status is not scoped to a principal, so
        # the only value it could carry is whichever profile read happened to
        # finish last -- possibly another principal's.
        return {
            **asdict(status),
            "data_exists": await asyncio.to_thread(self._data_exists),
            "recorder": await self._recorder_status_payload(),
        }

    async def _recorder_status_payload(self) -> dict[str, str | None]:
        if self._recorder_health.get("reason") == "call_log_corrupt":
            return dict(self._recorder_health)
        if not (
            self._config.enabled
            and self._config.diagnostics.log_provider_calls
        ):
            return dict(self._recorder_health)
        if not (
            self._process_records_calls
            and self._process is not None
            and self._process.running
        ):
            return dict(_RECORDER_DEGRADED)
        try:
            health = await self._provider.recorder_health()
        except Exception:
            health = dict(_RECORDER_DEGRADED)
        if health.get("state") == "disabled":
            # Diagnostics was explicitly enabled. A live sidecar with its
            # recorder off is a writer failure, not an intentional disable.
            health = dict(_RECORDER_DEGRADED)
        self._recorder_health = dict(health)
        return dict(self._recorder_health)

    async def failure_log_payload(self) -> dict[str, Any]:
        if not self.available:
            raise self._unavailable()
        entries = await self.module.failure_log()
        return {
            "items": [asdict(entry) for entry in entries],
            "retention_days": TERMINAL_TOMBSTONE_RETENTION.days,
        }

    def principal_for_user_key(self, user_key: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.principal_for_user_key(user_key)

    def project_for_workdir(self, workdir: str) -> str:
        if not self.available:
            raise self._unavailable()
        return self._store.project_for_workdir(workdir)

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
        limit: int,
        principal_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        if not self.available:
            return {"status": "failed", "error": "memory_store_unavailable"}
        return _result_payload(
            await self.module.search(
                query,
                limit=limit,
                principal_id=principal_id,
                project_id=project_id,
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

    async def _run_insight_read(
        self,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        async with self.module._lifecycle_lock:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(task)
                except Exception:
                    pass
                raise

    async def clear(self) -> dict[str, Any]:
        if not self.available:
            raise self._unavailable()
        result = await self.module.clear()
        if isinstance(result, ClearCompleted) and self._config.enabled:
            try:
                await self.reconcile(self._config)
            except Exception:
                # The durable clear already completed. A subsequent restart
                # problem is represented by status, never by rewriting the
                # completed clear receipt into a failure.
                self._runtime_error = "memory_sidecar_unavailable"
        return _clear_payload(result)

    async def install_artifact(self) -> dict[str, Any]:
        """Install or repair EverOS through this controller-owned lifecycle."""

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
        try:
            payload = await asyncio.to_thread(self._artifact_manager.ensure, force=True)
        except Exception:
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

    async def close(self) -> None:
        if self.available:
            await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False
        await self._stop_call_log_retention()
        self._artifact_manager.set_activation_coordinator(None)

    async def _apply_active_artifact_metadata(self) -> None:
        provider_root_format = await asyncio.to_thread(self._artifact_manager.provider_root_format)
        artifact_fingerprint = await asyncio.to_thread(self._artifact_manager.artifact_fingerprint)
        self.module._set_runtime_artifact_metadata(
            provider_root_format=provider_root_format or f"everos-{EVEROS_VERSION}",
            artifact_fingerprint=artifact_fingerprint or "memory-runtime-unavailable",
            compatible_provider_root_formats=_active_compatible_root_formats(self._artifact_manager),
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
            recovery = await self.module._recover_interrupted_clear()
            if isinstance(recovery, OperationFailed):
                self._runtime_error = recovery.error
                raise MemoryRuntimeActivationError("memory clear recovery failed")
            async with self.module._lifecycle_lock:
                previous_metadata = (
                    self.module._provider_root_format,
                    self.module._artifact_fingerprint,
                    self.module._compatible_provider_root_formats,
                )
                meta = None
                sentinel_rewritten = False
                try:
                    if not await self.module._worker.pause_and_wait():
                        raise MemoryRuntimeActivationError("memory worker could not pause")
                    await self._stop_worker()
                    if self._process is not None:
                        await self._process.stop()
                        self._process = None
                    self._process_records_calls = False
                    if root_state.exists:
                        meta = await asyncio.to_thread(self._store.get_meta)
                        if meta is None:
                            raise MemoryRuntimeActivationError("memory provider root metadata is missing")
                    self.module._set_runtime_artifact_metadata(
                        provider_root_format=candidate.provider_root_format,
                        artifact_fingerprint=candidate.artifact_fingerprint,
                        compatible_provider_root_formats=candidate.compatible_provider_root_formats,
                    )
                    if root_state.exists and root_state.empty and meta is not None:
                        sentinel_rewritten = await asyncio.to_thread(
                            self.module._activate_empty_provider_root_format,
                            meta,
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
                    return
                except (Exception, asyncio.CancelledError) as activation_error:
                    try:
                        rollback()
                        self.module._restore_runtime_artifact_metadata(previous_metadata)
                        if sentinel_rewritten and meta is not None:
                            await asyncio.to_thread(self.module._write_root_sentinel, meta)
                            await asyncio.to_thread(self.module._verify_owned_provider_root, meta, require_empty=True)
                        rollback_result = await self._reconcile_locked(
                            self._config,
                            claims_already_paused=True,
                            skip_embedding_guard=not self._config.embedding_change_pending,
                            resume_claims_on_failure=False,
                        )
                        if rollback_result.get("ok") is not True:
                            raise MemoryRuntimeActivationError("previous runtime reconciliation failed")
                    except Exception as rollback_error:
                        self._runtime_error = "memory_runtime_install_failed"
                        raise MemoryRuntimeActivationError("memory runtime rollback failed") from rollback_error
                    if isinstance(activation_error, asyncio.CancelledError):
                        raise
                    raise MemoryRuntimeActivationError("memory runtime activation failed") from activation_error

    async def _stop_sidecar_for_clear(self) -> None:
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        self._process_records_calls = False
        await self._stop_call_log_retention()
        await asyncio.to_thread(clear_call_log, self._call_log_db_path)
        self._recorder_health = dict(_RECORDER_DISABLED)

    async def _on_sidecar_ready(self) -> None:
        """Resume capture when a supervised child recovers after a failed boot."""

        if not self._config.enabled:
            return
        if self._config.diagnostics.log_provider_calls:
            self._process_records_calls = True
            await self._stop_call_log_retention()
        else:
            self._process_records_calls = False
            self._ensure_call_log_retention()
        self._runtime_error = None
        self.module._worker.resume_claims()
        self._ensure_worker()

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
        if self._worker_task is None or self._worker_task.done():
            self.module._worker.begin_activation()
            self._worker_task = asyncio.create_task(self._drain_loop(), name="memory-drain")

    async def _stop_worker(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _ensure_call_log_retention(self) -> None:
        task = self._call_log_retention_task
        if self._process_records_calls or not self._call_log_exists():
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
                    self._recorder_health = {"state": "degraded", "reason": reason}
                elif self._recorder_health.get("reason") != "call_log_corrupt":
                    self._recorder_health = dict(_RECORDER_DISABLED)
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
        task = asyncio.create_task(
            asyncio.to_thread(maintain_call_log, self._call_log_db_path)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise
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
            self._recorder_health = dict(_RECORDER_DISABLED)

    async def _drain_loop(self) -> None:
        while self._config.enabled:
            try:
                await self.module._worker.drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Memory drain activation failed; retrying recovery")
                self.module._worker.begin_activation()
            await asyncio.sleep(1.0)

    def _data_exists(self) -> bool:
        """Return a conservative status projection of provider, queue, and diagnostics."""

        try:
            return self._provider_data_exists_strict() or self._call_log_exists()
        except Exception:
            return True

    def _provider_data_exists_strict(self) -> bool:
        """Inspect all vector-bearing state, raising when it cannot be proven empty."""

        root = self._provider_root
        try:
            info = root.lstat()
        except FileNotFoundError:
            root_has_data = False
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("provider root is not a safe directory")
            with os.scandir(root) as entries:
                root_has_data = any(entry.name not in PROVIDER_ROOT_CONTROL_FILES for entry in entries)
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


def _result_payload(result: MemoryResult) -> dict[str, Any]:
    if isinstance(result, OperationFailed):
        return {"status": result.status, "error": result.error}
    if isinstance(result, MemoryItems):
        return {
            "status": result.status,
            "items": [memory_item_payload(item) for item in result.items],
            "warnings": list(result.warnings),
        }
    return {"status": "failed", "error": "memory_processing_failed"}


def _clear_payload(result: ClearCompleted | OperationFailed) -> dict[str, Any]:
    if isinstance(result, ClearCompleted):
        return {"status": result.status, "epoch": result.epoch}
    return {"status": result.status, "error": result.error}
