"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
import os
import stat
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, replace
from pathlib import Path
from collections.abc import Callable
from typing import Any

from config import paths
from config.v2_config import CONFIG_LOCK, MemoryConfig, V2Config
from core.memory.artifact import (
    MemoryArtifactCandidate,
    MemoryArtifactManager,
    MemoryProviderRootState,
    MemoryRuntimeActivationError,
    get_memory_artifact_manager,
)
from core.memory.everos import EverOSPort
from core.memory.module import MemoryModule
from core.memory.process import EverOSProcess, EverOSProcessSettings
from core.memory.store import MemoryStore
from core.memory.types import ClearCompleted, MemoryItems, MemoryResult, MemoryStatus, OperationFailed


_PROVIDER_ROOT_CONTROL_FILES = frozenset({".avibe-memory-root.json", "everos.toml", "ome.toml"})


class MemoryRuntime:
    """Own local Memory state, sidecar reconciliation, and periodic draining."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        store: MemoryStore | None = None,
        artifact_manager: MemoryArtifactManager | None = None,
        effective_home: Path | None = None,
    ) -> None:
        self._config = config
        self._effective_home = effective_home or paths.get_vibe_remote_dir()
        self._artifact_manager = artifact_manager or get_memory_artifact_manager()
        self._store = store or MemoryStore()
        self._process: EverOSProcess | None = None
        # The controller-side port only talks to the private UDS. Credentials
        # enter an EverOSPort only inside the owned child probe/sidecar.
        self._provider = EverOSPort(self._socket_path)
        self._runtime_error: str | None = None
        self._reconcile_lock = asyncio.Lock()
        self._processing_probe_lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None
        self._activation_loop: asyncio.AbstractEventLoop | None = None
        self._artifact_installing = False
        set_provider_root = getattr(self._artifact_manager, "set_provider_root", None)
        if callable(set_provider_root):
            set_provider_root(self._provider_root)
        self.module = MemoryModule(
            self._store,
            self._provider,
            enabled=lambda: self._config.enabled,
            runtime_error=lambda: self._runtime_error,
            starting=lambda: bool(self._process and self._process.starting),
            provider_root=self._provider_root,
            clear_provider_data=self._stop_sidecar_for_clear,
            provider_root_format=self._artifact_manager.provider_root_format() or "everos-1.1.3",
            artifact_fingerprint=self._artifact_manager.artifact_fingerprint() or "memory-runtime-unavailable",
            compatible_provider_root_formats=_active_compatible_root_formats(self._artifact_manager),
        )
        set_activation_coordinator = getattr(self._artifact_manager, "set_activation_coordinator", None)
        if callable(set_activation_coordinator):
            set_activation_coordinator(self._coordinate_artifact_activation)

    @property
    def _memory_dir(self) -> Path:
        return self._effective_home / "memory"

    @property
    def _provider_root(self) -> Path:
        return self._memory_dir / "everos-root"

    @property
    def _socket_path(self) -> Path:
        return self._memory_dir / ".rt" / "everos.sock"

    async def reconcile(self, config: MemoryConfig) -> dict[str, Any]:
        """Apply persisted config without restarting the Avibe service."""

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

        embedding_changed = not skip_embedding_guard and (
            config.embedding_change_pending or _embedding_configuration_changed(self._config, config)
        )
        claims_paused = claims_already_paused
        if embedding_changed:
            # Stop the worker before inspecting provider state. A capture may
            # still enqueue while settings are being reconciled, but no
            # old-embedding drain can cross this boundary.
            if not await self.module._worker.pause_and_wait():
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
            self._provider = EverOSPort(self._socket_path)
            self.module._replace_provider(self._provider)
            await self._stop_worker()
            if self._process is not None:
                await self._process.stop()
                self._process = None
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
            self._runtime_error = "memory_clear_failed"
            return {"ok": False, "error": self._runtime_error}
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None

        self._config = config
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

        self._process = EverOSProcess(
            python,
            provider_root=self._provider_root,
            effective_home=self._effective_home,
            owner_id=meta.principal_id,
            settings=_process_settings(config),
            socket_path=self._socket_path,
            on_ready=self._on_sidecar_ready,
        )
        started = await self._process.start()
        if not started:
            self._runtime_error = "memory_sidecar_unavailable"
            return {"ok": False, "error": self._runtime_error}
        self._runtime_error = None
        self.module._worker.resume_claims()
        self._ensure_worker()
        return {"ok": True, "state": "ready"}

    async def status_payload(self) -> dict[str, Any]:
        status = await self.module.status()
        return {
            **asdict(status),
            "profile_warning": "empty" if self._provider.profile_empty_warning else None,
            "data_exists": await asyncio.to_thread(self._data_exists),
        }

    async def profile_payload(self) -> dict[str, Any]:
        result = await self.module.profile()
        return {
            **_result_payload(result),
            "profile_warning": "empty" if self._provider.profile_empty_warning else None,
        }

    async def search_payload(self, query: str, limit: int) -> dict[str, Any]:
        return _result_payload(await self.module.search(query, limit=limit))

    async def clear(self) -> dict[str, Any]:
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
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None
        set_activation_coordinator = getattr(self._artifact_manager, "set_activation_coordinator", None)
        if callable(set_activation_coordinator):
            set_activation_coordinator(None)

    async def _apply_active_artifact_metadata(self) -> None:
        provider_root_format = await asyncio.to_thread(self._artifact_manager.provider_root_format)
        artifact_fingerprint = await asyncio.to_thread(self._artifact_manager.artifact_fingerprint)
        self.module._set_runtime_artifact_metadata(
            provider_root_format=provider_root_format or "everos-1.1.3",
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
        future = asyncio.run_coroutine_threadsafe(
            self._activate_artifact_candidate(candidate, root_state, commit, rollback),
            loop,
        )
        try:
            future.result(timeout=90.0)
        except FutureTimeoutError as timeout_error:
            # ``cancel`` only requests cancellation. Wait until the submitted
            # lifecycle transaction has settled so it cannot commit/restart in
            # the background after the installer reports a timeout.
            future.cancel()
            try:
                future.result()
            except FutureCancelledError as exc:
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

    async def _on_sidecar_ready(self) -> None:
        """Resume capture when a supervised child recovers after a failed boot."""

        if not self._config.enabled:
            return
        self._runtime_error = None
        self.module._worker.resume_claims()
        self._ensure_worker()

    async def _processing_healthy(self) -> bool:
        async with self._processing_probe_lock:
            process = self._process
            return bool(process is not None and await process.processing_healthy())

    async def _probe_processing(self, python: Path, config: MemoryConfig) -> bool:
        """Run the enablement probe under the controller-wide probe lock."""

        async with self._processing_probe_lock:
            probe_process = EverOSProcess(
                python,
                provider_root=self._provider_root,
                effective_home=self._effective_home,
                owner_id="memory-probe",
                settings=_process_settings(config),
                socket_path=self._socket_path,
            )
            return await probe_process.processing_healthy()

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
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

    async def _drain_loop(self) -> None:
        try:
            while self._config.enabled:
                await self.module._worker.drain()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise

    def _data_exists(self) -> bool:
        """Return a conservative status projection of provider/queue state."""

        try:
            return self._provider_data_exists_strict()
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
                root_has_data = any(entry.name not in _PROVIDER_ROOT_CONTROL_FILES for entry in entries)
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


def _active_compatible_root_formats(artifact_manager: object) -> tuple[str, ...]:
    getter = getattr(artifact_manager, "compatible_provider_root_formats", None)
    if not callable(getter):
        return ()
    try:
        values = getter()
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


def _same_memory_configuration(current: MemoryConfig, candidate: MemoryConfig) -> bool:
    """Compare persisted candidates while ignoring their settlement marker."""

    return (
        replace(current, embedding_change_pending=False)
        == replace(candidate, embedding_change_pending=False)
    )


def _process_settings(config: MemoryConfig) -> EverOSProcessSettings:
    return EverOSProcessSettings(**_provider_kwargs(config))


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
            "items": [asdict(item) for item in result.items],
            "warnings": list(result.warnings),
        }
    return {"status": "failed", "error": "memory_processing_failed"}


def _clear_payload(result: ClearCompleted | OperationFailed) -> dict[str, Any]:
    if isinstance(result, ClearCompleted):
        return {"status": result.status, "epoch": result.epoch}
    return {"status": result.status, "error": result.error}
