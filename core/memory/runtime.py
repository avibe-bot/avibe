"""Controller-owned orchestration for the local EverOS Memory runtime."""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

from config import paths
from config.v2_config import MemoryConfig
from core.memory.artifact import MemoryArtifactManager, get_memory_artifact_manager
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
        )

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
            # This is deliberately the same lifecycle lock Clear uses. A settings
            # save cannot race a root wipe or replace sidecar credentials halfway
            # through an active provider call.
            async with self.module._lifecycle_lock:
                if not config.enabled:
                    self._config = config
                    self._provider = EverOSPort(self._socket_path)
                    self.module._replace_provider(self._provider)
                    await self._stop_worker()
                    if self._process is not None:
                        await self._process.stop()
                        self._process = None
                    self._runtime_error = None
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
                    error = _runtime_error_for_status(
                        await asyncio.to_thread(self._artifact_manager.status)
                    )
                    if not (self._process and self._process.running):
                        self._runtime_error = error
                    return {"ok": False, "error": error}
                if not await self._probe_processing(python, config):
                    error = "memory_processing_failed"
                    if not (self._process and self._process.running):
                        self._runtime_error = error
                    return {"ok": False, "error": error}

                # Every enabled reconciliation receives a fresh process. Endpoint,
                # model, and key changes belong exclusively in its allowlisted
                # child environment and must never leave an old sidecar running.
                await self.module._worker.pause_and_wait()
                await self._stop_worker()
                if self._process is not None:
                    await self._process.stop()
                    self._process = None

                self._config = config
                self._provider = candidate_provider
                self.module._replace_provider(self._provider)

                self.module._provider_root_format = (
                    await asyncio.to_thread(self._artifact_manager.provider_root_format)
                ) or "everos-1.1.3"
                self.module._artifact_fingerprint = (
                    await asyncio.to_thread(self._artifact_manager.artifact_fingerprint)
                ) or "memory-runtime-unavailable"
                try:
                    meta = await asyncio.to_thread(self._store.ensure_meta)
                    await asyncio.to_thread(self.module._ensure_owned_provider_root, meta)
                except Exception:
                    self._runtime_error = "memory_clear_failed"
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

    async def close(self) -> None:
        await self._stop_worker()
        if self._process is not None:
            await self._process.stop()
            self._process = None

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
        try:
            root = self._provider_root
            info = root.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return False
            for entry in os.scandir(root):
                if entry.name not in _PROVIDER_ROOT_CONTROL_FILES:
                    return True
            stats = self._store.queue_stats()
            return bool(stats.pending or stats.processing or stats.dead or self._store.has_provider_data_history())
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
