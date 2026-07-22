"""The five-method, provider-independent MemoryModule interface."""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import stat
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from config import paths
from core.memory.everos import MemoryProviderFailure, MemoryProviderPort
from core.memory.store import MAX_NONTERMINAL_QUEUE_ROWS, MemoryMeta, MemoryStore, QueueStats
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
CLEAR_DRAIN_TIMEOUT_SECONDS = 5.0
CLEAR_CLEANUP_TIMEOUT_SECONDS = 20.0
MAX_PROVIDER_DISK_ENTRIES = 100_000


class _ClearStepFailure(RuntimeError):
    """Internal signal used to retain the durable clear-recovery marker."""


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
        clear_drain_timeout_seconds: float = CLEAR_DRAIN_TIMEOUT_SECONDS,
        clear_cleanup_timeout_seconds: float = CLEAR_CLEANUP_TIMEOUT_SECONDS,
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
        self._clear_drain_timeout_seconds = _positive_timeout(clear_drain_timeout_seconds)
        self._clear_cleanup_timeout_seconds = _positive_timeout(clear_cleanup_timeout_seconds)
        self._lifecycle_lock = asyncio.Lock()
        self._clear_active = False
        self._worker = worker or MemoryWorker(
            store=store,
            provider=provider,
            enabled=self._is_enabled,
        )

    async def capture(self, request: CaptureRequest) -> CaptureReceipt:
        """Validate and persist one source capture without touching the provider."""

        if not self._is_enabled():
            return CaptureSkipped(reason="memory_disabled")
        if self._clear_active:
            return CaptureSkipped(reason="memory_clear_failed")
        if not isinstance(request, CaptureRequest):
            return await self._skipped_with_missed("memory_invalid_input")

        normalized_text = self._normalize_text(request.text)
        validation_error = self._capture_validation_error(request, normalized_text)
        if validation_error is not None:
            return await self._skipped_with_missed(validation_error)

        try:
            disk_free = int(await asyncio.to_thread(self._disk_free_bytes))
        except Exception:
            return await self._skipped_with_missed("memory_low_disk_space")
        if disk_free < MIN_FREE_DISK_BYTES:
            return await self._skipped_with_missed("memory_low_disk_space")

        try:
            result = await self._store_call(
                self._store.enqueue_request,
                source_message_id=request.source_message_id,
                session_id=request.session_id,
                payload_text=normalized_text,
                occurred_at_ms=request.occurred_at_ms,
                max_provider_timestamp_ms=MAX_PROVIDER_TIMESTAMP_MS,
                nonterminal_limit=MAX_NONTERMINAL_QUEUE_ROWS,
            )
        except UnicodeError:
            return await self._skipped_with_missed("memory_invalid_input")
        except Exception:
            return OperationFailed(error="memory_store_unavailable")

        if result.outcome == "accepted":
            return CaptureAccepted()
        if result.outcome == "duplicate":
            return CaptureDuplicate()
        if result.outcome == "queue_full":
            return CaptureSkipped(reason="memory_queue_full")
        if result.outcome == "timestamp_invalid":
            return CaptureSkipped(reason="memory_invalid_input")
        return CaptureSkipped(reason="memory_clear_failed")

    async def search(self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> MemoryResult:
        """Return a bounded provider search result or one closed error category."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        normalized_query = self._normalize_text(query)
        query_bytes = _utf8_bytes(normalized_query)
        if query_bytes is None or not normalized_query.strip():
            return OperationFailed(error="memory_invalid_input")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEARCH_LIMIT:
            return OperationFailed(error="memory_invalid_input")
        if len(query_bytes) > MAX_QUERY_BYTES:
            return OperationFailed(error="memory_input_too_large")

        recovery = await self._recover_interrupted_clear()
        if recovery is not None:
            return recovery
        if self._clear_active:
            return OperationFailed(error="memory_clear_failed")

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
        """Return a bounded provider profile result or one closed error category."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        recovery = await self._recover_interrupted_clear()
        if recovery is not None:
            return recovery
        if self._clear_active:
            return OperationFailed(error="memory_clear_failed")

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
        return result if isinstance(result, OperationFailed) else self._bounded_items(
            result,
            limit=MAX_PROVIDER_RESULT_ITEMS,
        )

    async def status(self) -> MemoryStatus:
        """Return status using the frozen precedence order."""

        if not self._clear_active:
            await self._recover_interrupted_clear()
        try:
            meta = await self._store_call(self._store.get_meta)
            stats = await self._store_call(self._store.queue_stats)
        except Exception:
            return MemoryStatus(state="error", error="memory_store_unavailable")

        if self._clear_active or (meta is not None and meta.clear_in_progress):
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
        """Run one idempotent, bounded clear lifecycle operation."""

        async with self._lifecycle_lock:
            self._clear_active = True
            try:
                started = await self._store_call(self._store.begin_clear)
                if not await self._worker.pause_and_wait(
                    timeout_seconds=self._clear_drain_timeout_seconds
                ):
                    raise _ClearStepFailure("worker drain did not stop in time")
                await self._clear_provider_data_or_fail()
                completed = await self._store_call(self._store.finish_clear)
            except Exception:
                await self._record_clear_failure()
                return OperationFailed(error="memory_clear_failed")
            finally:
                self._clear_active = False

            self._worker.resume_claims()
            return ClearCompleted(epoch=completed.epoch if completed is not None else started.epoch)

    async def _recover_interrupted_clear(self) -> OperationFailed | None:
        """Retry a durable clear marker on every eligible lifecycle check."""

        if self._clear_active:
            return None
        async with self._lifecycle_lock:
            if self._clear_active:
                return None
            try:
                meta = await self._store_call(self._store.get_meta)
            except Exception:
                return OperationFailed(error="memory_clear_failed")
            if meta is None or not meta.clear_in_progress:
                self._worker.resume_claims()
                return None

            try:
                if not await self._worker.pause_and_wait(
                    timeout_seconds=self._clear_drain_timeout_seconds
                ):
                    raise _ClearStepFailure("worker drain did not stop in time")
                await self._clear_provider_data_or_fail()
                await self._store_call(self._store.finish_clear)
            except Exception:
                await self._record_clear_failure()
                return OperationFailed(error="memory_clear_failed")

            self._worker.resume_claims()
            return None

    async def _skipped_with_missed(self, error: MemoryErrorCode) -> CaptureReceipt:
        try:
            status_error = error if error == "memory_low_disk_space" else None
            await self._store_call(self._store.record_capture_skip, status_error)
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
            return OperationFailed(error=_provider_error_code(failure, "memory_processing_failed"))
        except Exception:
            return OperationFailed(error="memory_processing_failed")

    def _bounded_items(self, items: tuple[MemoryItem, ...], *, limit: int) -> MemoryResult:
        if not isinstance(items, tuple) or len(items) > limit:
            return OperationFailed(error="memory_provider_response_invalid")
        total_bytes = 0
        for item in items:
            if not isinstance(item, MemoryItem) or item.kind not in {"profile", "episode", "fact"}:
                return OperationFailed(error="memory_provider_response_invalid")
            item_text = _utf8_bytes(item.text) if isinstance(item.text, str) else None
            if item_text is None or not item.text or "\x00" in item.text:
                return OperationFailed(error="memory_provider_response_invalid")
            if len(item_text) > MAX_PROVIDER_ITEM_BYTES:
                return OperationFailed(error="memory_provider_response_invalid")
            total_bytes += len(item_text) + len(item.kind.encode("utf-8"))
            if item.date is not None:
                date_bytes = _utf8_bytes(item.date) if isinstance(item.date, str) else None
                if date_bytes is None or len(date_bytes) > 64:
                    return OperationFailed(error="memory_provider_response_invalid")
                try:
                    date.fromisoformat(item.date)
                except ValueError:
                    return OperationFailed(error="memory_provider_response_invalid")
                total_bytes += len(date_bytes)
            if total_bytes > MAX_PROVIDER_RESULT_BYTES:
                return OperationFailed(error="memory_provider_response_invalid")
        return MemoryItems(items=items)

    def _capture_validation_error(
        self,
        request: CaptureRequest,
        normalized_text: str,
    ) -> MemoryErrorCode | None:
        if not isinstance(request.source_message_id, str) or not isinstance(request.session_id, str):
            return "memory_invalid_input"
        if not self._valid_identifier(request.source_message_id) or not self._valid_identifier(request.session_id):
            return "memory_invalid_input"
        if not isinstance(request.occurred_at_ms, int) or isinstance(request.occurred_at_ms, bool):
            return "memory_invalid_input"
        if request.occurred_at_ms < 0 or request.occurred_at_ms > MAX_PROVIDER_TIMESTAMP_MS:
            return "memory_invalid_input"
        text_bytes = _utf8_bytes(normalized_text)
        if text_bytes is None or not normalized_text.strip() or self._is_memory_command(normalized_text):
            return "memory_invalid_input"
        if len(text_bytes) > MAX_CAPTURE_TEXT_BYTES:
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
        meta: MemoryMeta | None,
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

    async def _clear_provider_data_or_fail(self) -> None:
        if self._clear_provider_data is None:
            raise _ClearStepFailure("provider clear dependency is unavailable")
        try:
            async with asyncio.timeout(self._clear_cleanup_timeout_seconds):
                result = await asyncio.to_thread(self._clear_provider_data)
                if inspect.isawaitable(result):
                    await result
        except TimeoutError as error:
            raise _ClearStepFailure("provider clear timed out") from error
        if not self._provider_root_is_empty():
            raise _ClearStepFailure("provider root was not cleared")

    async def _record_clear_failure(self) -> None:
        try:
            await self._store_call(self._store.set_last_error, "memory_clear_failed")
        except Exception:
            return

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def _default_free_disk_bytes(self) -> int:
        return int(shutil.disk_usage(self._store.path.parent).free)

    def _provider_disk_bytes(self) -> int:
        try:
            root_info = self._provider_root.lstat()
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
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
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += int(info.st_size)
        except OSError:
            return 0
        return total

    def _provider_root_is_empty(self) -> bool:
        try:
            root_info = self._provider_root.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            return False
        try:
            with os.scandir(self._provider_root) as entries:
                return next(entries, None) is None
        except OSError:
            return False

    @staticmethod
    def _normalize_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        try:
            return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
        except (TypeError, UnicodeError, ValueError):
            return ""

    @staticmethod
    def _valid_identifier(value: str) -> bool:
        encoded = _utf8_bytes(value)
        return bool(value.strip()) and encoded is not None and len(encoded) <= MAX_CAPTURE_IDENTIFIER_BYTES

    @staticmethod
    def _is_memory_command(value: str) -> bool:
        command = value.strip().casefold()
        return command == "/memory" or command.startswith("/memory ")


def _provider_error_code(error: MemoryProviderFailure, fallback: MemoryErrorCode) -> MemoryErrorCode:
    return error.error if is_memory_error_code(error.error) else fallback


def _utf8_bytes(value: str) -> bytes | None:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        return None


def _positive_timeout(value: float) -> float:
    try:
        return max(float(value), 0.001)
    except (TypeError, ValueError):
        return 0.001
