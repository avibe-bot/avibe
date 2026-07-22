"""The five-method, provider-independent MemoryModule interface."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import os
import shutil
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from config import paths
from core.memory.everos import MemoryProviderFailure, MemoryProviderPort
from core.memory.store import MAX_NONTERMINAL_QUEUE_ROWS, MemoryStore, QueueStats
from core.memory.types import (
    CaptureAccepted,
    CaptureDuplicate,
    CaptureReceipt,
    CaptureRequest,
    CaptureSkipped,
    ClearCompleted,
    ClearReceipt,
    MemoryErrorCode,
    MemoryItem,
    MemoryItems,
    MemoryResult,
    MemoryStatus,
    OperationFailed,
    is_memory_error_code,
)
from core.memory.worker import MemoryWorker


MAX_CAPTURE_TEXT_BYTES = 32 * 1024
MAX_CAPTURE_IDENTIFIER_BYTES = 1024
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_TIMESTAMP_MS = 4_102_444_800_000
MAX_QUERY_BYTES = 8 * 1024
MAX_SEARCH_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 8
MAX_PROVIDER_ITEM_BYTES = 64 * 1024
MAX_PROVIDER_RESULT_BYTES = 256 * 1024
MAX_PROVIDER_RESULT_ITEMS = 20
PROVIDER_READ_TIMEOUT_SECONDS = 20.0
MAX_PROVIDER_DISK_ENTRIES = 100_000


class MemoryModule:
    """Own local capture, direct reads, status, and clear without exposing internals."""

    def __init__(
        self,
        store: MemoryStore,
        provider: MemoryProviderPort,
        *,
        enabled: bool | Callable[[], bool] = False,
        runtime_error: MemoryErrorCode | None | Callable[[], MemoryErrorCode | None] = None,
        starting: bool | Callable[[], bool] = False,
        disk_free_bytes: Callable[[], int] | None = None,
        provider_root: Path | None = None,
        clear_provider_data: Callable[[], Awaitable[None] | None] | None = None,
        worker: MemoryWorker | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled_source = enabled
        self._runtime_error_source = runtime_error
        self._starting_source = starting
        self._disk_free_bytes = disk_free_bytes or self._default_free_disk_bytes
        self._provider_root = provider_root or (paths.get_vibe_remote_dir() / "memory" / "everos-root")
        self._clear_provider_data = clear_provider_data
        self._lifecycle_lock = asyncio.Lock()
        self._clear_active = False
        try:
            self._clear_recovery_needed = store.clear_in_progress()
        except Exception:
            self._clear_recovery_needed = False
        self._worker = worker or MemoryWorker(
            store=store,
            provider=provider,
            enabled=self._is_enabled,
        )

    async def capture(self, request: CaptureRequest) -> CaptureReceipt:
        recovery = await self._recover_interrupted_clear()
        if recovery is not None:
            return recovery
        if not self._is_enabled():
            return CaptureSkipped(reason="memory_disabled")

        normalized_text = self._normalize_text(request.text)
        validation_error = self._capture_validation_error(request, normalized_text)
        if validation_error is not None:
            return await self._skipped_with_missed(validation_error)

        async with self._lifecycle_lock:
            if not self._is_enabled():
                return CaptureSkipped(reason="memory_disabled")
            try:
                meta = await self._store_call(self._store.ensure_meta)
            except Exception:
                return OperationFailed(error="memory_store_unavailable")
            if meta.clear_in_progress:
                return CaptureSkipped(reason="memory_clear_failed")

            try:
                disk_free = int(await asyncio.to_thread(self._disk_free_bytes))
            except Exception:
                return await self._skipped_with_missed("memory_low_disk_space")
            if disk_free < MIN_FREE_DISK_BYTES:
                return await self._skipped_with_missed("memory_low_disk_space")

            source_digest = self._keyed_digest(meta.scope_key, request.source_message_id)
            session_ref = self._provider_session_ref(meta.scope_key, request.session_id, meta.epoch)
            try:
                result = await self._store_call(
                    self._store.enqueue_capture,
                    source_message_digest=source_digest,
                    session_ref=session_ref,
                    payload_text=normalized_text,
                    occurred_at_ms=request.occurred_at_ms,
                    max_provider_timestamp_ms=MAX_PROVIDER_TIMESTAMP_MS,
                    nonterminal_limit=MAX_NONTERMINAL_QUEUE_ROWS,
                )
            except Exception:
                return OperationFailed(error="memory_store_unavailable")

            if result.outcome == "accepted":
                return CaptureAccepted()
            if result.outcome == "duplicate":
                return CaptureDuplicate()
            if result.outcome == "queue_full":
                return await self._skipped_with_missed("memory_queue_full")
            if result.outcome == "timestamp_invalid":
                return await self._skipped_with_missed("memory_invalid_input")
            return CaptureSkipped(reason="memory_clear_failed")

    async def search(self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> MemoryResult:
        recovery = await self._recover_interrupted_clear()
        if recovery is not None:
            return recovery
        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        normalized_query = self._normalize_text(query)
        if not normalized_query.strip() or not isinstance(limit, int) or isinstance(limit, bool):
            return OperationFailed(error="memory_invalid_input")
        if len(normalized_query.encode("utf-8")) > MAX_QUERY_BYTES:
            return OperationFailed(error="memory_input_too_large")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            return OperationFailed(error="memory_invalid_input")

        async with self._lifecycle_lock:
            if not self._is_enabled():
                return OperationFailed(error="memory_disabled")
            try:
                meta = await self._store_call(self._store.ensure_meta)
            except Exception:
                return OperationFailed(error="memory_store_unavailable")
            if meta.clear_in_progress:
                return OperationFailed(error="memory_clear_failed")
            result = await self._provider_read(
                lambda: self._provider.search(meta.principal_id, normalized_query, limit)
            )
        return result if isinstance(result, OperationFailed) else self._bounded_items(result, limit=limit)

    async def profile(self) -> MemoryResult:
        recovery = await self._recover_interrupted_clear()
        if recovery is not None:
            return recovery
        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")

        async with self._lifecycle_lock:
            if not self._is_enabled():
                return OperationFailed(error="memory_disabled")
            try:
                meta = await self._store_call(self._store.ensure_meta)
            except Exception:
                return OperationFailed(error="memory_store_unavailable")
            if meta.clear_in_progress:
                return OperationFailed(error="memory_clear_failed")
            result = await self._provider_read(lambda: self._provider.profile(meta.principal_id))
        return result if isinstance(result, OperationFailed) else self._bounded_items(result, limit=MAX_PROVIDER_RESULT_ITEMS)

    async def status(self) -> MemoryStatus:
        await self._recover_interrupted_clear()
        try:
            meta = await self._store_call(self._store.get_meta)
            stats = await self._store_call(self._store.queue_stats)
        except Exception:
            return MemoryStatus(state="error", error="memory_store_unavailable")

        if meta is not None and meta.clear_in_progress:
            return await self._status("clearing", meta=meta, stats=stats)
        if not self._is_enabled():
            return await self._status("disabled", meta=meta, stats=stats)
        runtime_error = self._runtime_error()
        if runtime_error is not None:
            return await self._status("error", meta=meta, stats=stats, error=runtime_error)
        if self._is_starting():
            return await self._status("starting", meta=meta, stats=stats)
        if not await self._provider_healthy():
            return await self._status(
                "down",
                meta=meta,
                stats=stats,
                error=(meta.last_error if meta is not None else None) or "memory_sidecar_unavailable",
            )
        if (meta is not None and meta.last_error is not None) or stats.dead:
            return await self._status("degraded", meta=meta, stats=stats)
        if stats.pending or stats.processing:
            return await self._status("indexing", meta=meta, stats=stats)
        return await self._status("ready", meta=meta, stats=stats)

    async def clear(self) -> ClearReceipt:
        async with self._lifecycle_lock:
            self._clear_active = True
            try:
                meta = await self._store_call(self._store.begin_clear)
                self._clear_recovery_needed = True
                await self._worker.pause_and_wait()
                await self._clear_provider_data_if_needed()
                completed = await self._store_call(self._store.finish_clear)
            except Exception:
                try:
                    await self._store_call(self._store.set_last_error, "memory_clear_failed")
                except Exception:
                    pass
                return OperationFailed(error="memory_clear_failed")
            finally:
                self._clear_active = False
            self._clear_recovery_needed = False
            if self._is_enabled():
                self._worker.resume_claims()
            return ClearCompleted(epoch=completed.epoch if completed is not None else meta.epoch)

    async def _recover_interrupted_clear(self) -> OperationFailed | None:
        if self._clear_active:
            return None
        if not self._clear_recovery_needed:
            return None
        async with self._lifecycle_lock:
            try:
                meta = await self._store_call(self._store.get_meta)
                if meta is None or not meta.clear_in_progress:
                    self._clear_recovery_needed = False
                    if self._is_enabled():
                        self._worker.resume_claims()
                    return None
                await self._worker.pause_and_wait()
                await self._clear_provider_data_if_needed()
                await self._store_call(self._store.finish_clear)
            except Exception:
                try:
                    await self._store_call(self._store.set_last_error, "memory_clear_failed")
                except Exception:
                    pass
                return OperationFailed(error="memory_clear_failed")
            self._clear_recovery_needed = False
            if self._is_enabled():
                self._worker.resume_claims()
            return None

    async def _skipped_with_missed(self, error: MemoryErrorCode) -> CaptureReceipt:
        try:
            await self._store_call(self._store.increment_missed)
        except Exception:
            return OperationFailed(error="memory_store_unavailable")
        return CaptureSkipped(reason=error)

    async def _provider_read(
        self,
        operation: Callable[[], Awaitable[tuple[MemoryItem, ...]]],
    ) -> tuple[MemoryItem, ...] | OperationFailed:
        try:
            return await asyncio.wait_for(operation(), timeout=PROVIDER_READ_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return OperationFailed(error="memory_provider_timeout")
        except MemoryProviderFailure as failure:
            return OperationFailed(error=failure.error)
        except Exception:
            return OperationFailed(error="memory_processing_failed")

    def _bounded_items(self, items: tuple[MemoryItem, ...], *, limit: int) -> MemoryResult:
        if not isinstance(items, tuple) or len(items) > limit:
            return OperationFailed(error="memory_provider_response_invalid")
        total_bytes = 0
        for item in items:
            if not isinstance(item, MemoryItem) or item.kind not in {"profile", "episode", "fact"}:
                return OperationFailed(error="memory_provider_response_invalid")
            if not isinstance(item.text, str) or not item.text or "\x00" in item.text:
                return OperationFailed(error="memory_provider_response_invalid")
            item_bytes = len(item.text.encode("utf-8"))
            if item_bytes > MAX_PROVIDER_ITEM_BYTES:
                return OperationFailed(error="memory_provider_response_invalid")
            total_bytes += item_bytes + len(item.kind.encode("utf-8"))
            if item.date is not None:
                if not isinstance(item.date, str) or len(item.date.encode("utf-8")) > 64:
                    return OperationFailed(error="memory_provider_response_invalid")
                try:
                    date.fromisoformat(item.date)
                except ValueError:
                    return OperationFailed(error="memory_provider_response_invalid")
                total_bytes += len(item.date.encode("utf-8"))
            if total_bytes > MAX_PROVIDER_RESULT_BYTES:
                return OperationFailed(error="memory_provider_response_invalid")
        return MemoryItems(items=items)

    def _capture_validation_error(self, request: CaptureRequest, normalized_text: str) -> MemoryErrorCode | None:
        if not isinstance(request.source_message_id, str) or not isinstance(request.session_id, str):
            return "memory_invalid_input"
        if not self._valid_identifier(request.source_message_id) or not self._valid_identifier(request.session_id):
            return "memory_invalid_input"
        if not isinstance(request.occurred_at_ms, int) or isinstance(request.occurred_at_ms, bool):
            return "memory_invalid_input"
        if request.occurred_at_ms < 0 or request.occurred_at_ms > MAX_PROVIDER_TIMESTAMP_MS:
            return "memory_invalid_input"
        if not normalized_text.strip() or self._is_memory_command(normalized_text):
            return "memory_invalid_input"
        if len(normalized_text.encode("utf-8")) > MAX_CAPTURE_TEXT_BYTES:
            return "memory_input_too_large"
        return None

    async def _status(
        self,
        state: Literal[
            "disabled",
            "starting",
            "ready",
            "indexing",
            "degraded",
            "down",
            "clearing",
            "error",
        ],
        *,
        meta: Any,
        stats: QueueStats,
        error: MemoryErrorCode | None = None,
    ) -> MemoryStatus:
        return MemoryStatus(
            state=state,
            pending=stats.pending,
            processing=stats.processing,
            dead=stats.dead,
            missed=meta.missed_count if meta is not None else 0,
            queue_plaintext_bytes=stats.queue_plaintext_bytes,
            provider_disk_bytes=await asyncio.to_thread(self._provider_disk_bytes),
            last_success_at=meta.last_success_at if meta is not None else None,
            error=error if error is not None else (meta.last_error if meta is not None else None),
        )

    def _is_enabled(self) -> bool:
        try:
            value = self._enabled_source() if callable(self._enabled_source) else self._enabled_source
        except Exception:
            return False
        return bool(value)

    def _runtime_error(self) -> MemoryErrorCode | None:
        try:
            value = self._runtime_error_source() if callable(self._runtime_error_source) else self._runtime_error_source
        except Exception:
            return "memory_runtime_install_failed"
        return value if is_memory_error_code(value) else None

    def _is_starting(self) -> bool:
        try:
            value = self._starting_source() if callable(self._starting_source) else self._starting_source
        except Exception:
            return False
        return bool(value)

    async def _provider_healthy(self) -> bool:
        try:
            return bool(await asyncio.wait_for(self._provider.health(), timeout=PROVIDER_READ_TIMEOUT_SECONDS))
        except Exception:
            return False

    async def _clear_provider_data_if_needed(self) -> None:
        if self._clear_provider_data is None:
            return
        result = self._clear_provider_data()
        if inspect.isawaitable(result):
            await result

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def _default_free_disk_bytes(self) -> int:
        return int(shutil.disk_usage(self._store.path.parent).free)

    def _provider_disk_bytes(self) -> int:
        try:
            root_info = self._provider_root.lstat()
            if not self._provider_root.is_dir() or _is_link(root_info.st_mode):
                return 0
        except OSError:
            return 0

        total = 0
        visited = 0
        directories = [self._provider_root]
        try:
            while directories and visited < MAX_PROVIDER_DISK_ENTRIES:
                directory = directories.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if visited >= MAX_PROVIDER_DISK_ENTRIES:
                            break
                        visited += 1
                        info = entry.stat(follow_symlinks=False)
                        if _is_link(info.st_mode):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += int(info.st_size)
        except OSError:
            return 0
        return total

    @staticmethod
    def _normalize_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))

    @staticmethod
    def _valid_identifier(value: str) -> bool:
        return bool(value) and len(value.encode("utf-8")) <= MAX_CAPTURE_IDENTIFIER_BYTES

    @staticmethod
    def _is_memory_command(value: str) -> bool:
        command = value.strip().casefold()
        return command == "/memory" or command.startswith("/memory ")

    @staticmethod
    def _keyed_digest(scope_key: bytes, value: str) -> str:
        return hmac.new(scope_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @classmethod
    def _provider_session_ref(cls, scope_key: bytes, session_id: str, epoch: int) -> str:
        return f"src--{cls._keyed_digest(scope_key, session_id)}--e{epoch}"


def _is_link(mode: int) -> bool:
    return bool(mode & 0o170000 == 0o120000)
