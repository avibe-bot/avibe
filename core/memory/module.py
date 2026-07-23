"""The five-method, provider-independent MemoryModule interface."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import shutil
import stat
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
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
    MemoryFailureLogEntry,
    MemoryItem,
    MemoryItems,
    MemoryResult,
    MemoryStatus,
    OperationFailed,
    is_memory_error_code,
)
from core.memory.worker import MemoryWorker, ProcessingEvent


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
ROOT_SENTINEL_FILENAME = ".avibe-memory-root.json"
ROOT_SENTINEL_SCHEMA_VERSION = 1
ROOT_PROVIDER_ID = "everos"
SLICE1_PROVIDER_ROOT_FORMAT = "slice1"
SLICE1_ARTIFACT_FINGERPRINT = "slice1-core"
MAX_ROOT_SENTINEL_BYTES = 4 * 1024


_ROOT_LIFECYCLE_LOCKS: dict[str, asyncio.Lock] = {}
_ROOT_CLEANUP_TASKS: dict[str, asyncio.Task[None]] = {}


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
        provider_root_format: str = SLICE1_PROVIDER_ROOT_FORMAT,
        artifact_fingerprint: str = SLICE1_ARTIFACT_FINGERPRINT,
        compatible_provider_root_formats: Iterable[str] = (),
        clear_drain_timeout_seconds: float = CLEAR_DRAIN_TIMEOUT_SECONDS,
        clear_cleanup_timeout_seconds: float = CLEAR_CLEANUP_TIMEOUT_SECONDS,
        processing_event: ProcessingEvent | None = None,
        worker: MemoryWorker | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled_source = enabled
        self._runtime_error_source = runtime_error
        self._starting_source = starting
        self._disk_free_bytes = disk_free_bytes or self._default_free_disk_bytes
        self._provider_root = provider_root or (paths.get_vibe_remote_dir() / "memory" / "everos-root")
        self._provider_root_key = os.path.abspath(os.fspath(self._provider_root))
        self._effective_home = paths.get_vibe_remote_dir()
        self._provider_root_format = _root_metadata_value(
            provider_root_format,
            fallback=SLICE1_PROVIDER_ROOT_FORMAT,
        )
        self._artifact_fingerprint = _root_metadata_value(
            artifact_fingerprint,
            fallback=SLICE1_ARTIFACT_FINGERPRINT,
        )
        self._compatible_provider_root_formats = frozenset(
            {
                self._provider_root_format,
                *(
                    value
                    for value in compatible_provider_root_formats
                    if _is_root_metadata_value(value)
                ),
            }
        )
        self._clear_provider_data = clear_provider_data
        self._clear_drain_timeout_seconds = _positive_timeout(clear_drain_timeout_seconds)
        self._clear_cleanup_timeout_seconds = _positive_timeout(clear_cleanup_timeout_seconds)
        self._lifecycle_lock = asyncio.Lock()
        self._clear_active = False
        self._worker = worker or MemoryWorker(
            store=store,
            provider=provider,
            enabled=self._is_enabled,
            processing_event=processing_event,
        )

    def _replace_provider(self, provider: MemoryProviderPort) -> None:
        """Swap the private provider shared by direct reads and the worker.

        ``MemoryRuntime`` holds the module lifecycle lock before invoking this,
        so a sidecar credential/runtime replacement cannot split these two
        consumers across provider instances.
        """

        self._provider = provider
        self._worker._provider = provider

    def _set_runtime_artifact_metadata(
        self,
        *,
        provider_root_format: str,
        artifact_fingerprint: str,
        compatible_provider_root_formats: Iterable[str],
    ) -> tuple[str, str, frozenset[str]]:
        """Switch active artifact metadata while the runtime lifecycle is fenced."""

        previous = (
            self._provider_root_format,
            self._artifact_fingerprint,
            self._compatible_provider_root_formats,
        )
        self._provider_root_format = _root_metadata_value(
            provider_root_format,
            fallback=SLICE1_PROVIDER_ROOT_FORMAT,
        )
        self._artifact_fingerprint = _root_metadata_value(
            artifact_fingerprint,
            fallback=SLICE1_ARTIFACT_FINGERPRINT,
        )
        self._compatible_provider_root_formats = frozenset(
            {
                self._provider_root_format,
                *(value for value in compatible_provider_root_formats if _is_root_metadata_value(value)),
            }
        )
        return previous

    def _restore_runtime_artifact_metadata(self, previous: tuple[str, str, frozenset[str]]) -> None:
        self._provider_root_format, self._artifact_fingerprint, self._compatible_provider_root_formats = previous

    def _activate_empty_provider_root_format(self, meta: MemoryMeta) -> bool:
        """Rewrite only a verified empty sentinel when an artifact format changes."""

        try:
            self._provider_root.lstat()
        except FileNotFoundError:
            return False
        self._verify_owned_provider_root(meta, require_empty=False)
        sentinel = _read_root_sentinel(self._provider_root / ROOT_SENTINEL_FILENAME)
        current_format = sentinel.get("provider_root_format") if isinstance(sentinel, dict) else None
        if current_format == self._provider_root_format:
            return False
        self._verify_owned_provider_root(meta, require_empty=True)
        self._write_root_sentinel(meta)
        self._verify_owned_provider_root(meta, require_empty=True)
        return True

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
        # Compute the active persisted error once. A newer terminal flush
        # observation supersedes delivery-era errors from older builds, while a
        # later add failure (meta.updated_at > last_flush_at) remains current.
        historical = meta.last_error if meta is not None else None
        flush_supersedes_error = bool(
            meta is not None
            and stats.last_flush_at is not None
            and meta.last_error_at is not None
            and stats.last_flush_at >= meta.last_error_at
            and (
                historical in {"memory_sidecar_unavailable", "memory_provider_timeout"}
                or (
                    historical == "memory_processing_failed"
                    and meta.processing_fault_since is None
                )
            )
        )
        active_error = (
            None
            if historical == "memory_low_disk_space" or flush_supersedes_error
            else historical
        )
        if flush_supersedes_error and historical is not None and meta is not None:
            try:
                await self._store_call(
                    self._store.clear_superseded_error,
                    expected_error=historical,
                    expected_error_at=meta.last_error_at,
                )
            except Exception:
                pass
        runtime_error = self._runtime_error()
        if runtime_error is not None:
            return await self._status("error", meta=meta, stats=stats, error=runtime_error)
        if self._is_starting():
            return await self._status("starting", meta=meta, stats=stats)
        if not await self._provider_healthy():
            # An active provider outage is the cause; do not echo a stale persisted
            # last_error (e.g. a resolved memory_low_disk_space) that would misreport
            # the current condition (tech §15 precedence).
            return await self._status(
                "down",
                meta=meta,
                stats=stats,
                error=active_error or "memory_sidecar_unavailable",
            )
        if not await self._has_minimum_free_disk():
            return await self._status(
                "degraded",
                meta=meta,
                stats=stats,
                error="memory_low_disk_space",
            )
        if active_error is not None:
            return await self._status("degraded", meta=meta, stats=stats, error=active_error)
        if stats.pending or stats.processing or stats.awaiting_receipt:
            return await self._status("syncing", meta=meta, stats=stats, error=None)
        return await self._status("ready", meta=meta, stats=stats, error=None)

    async def failure_log(self, *, limit: int = 50) -> tuple[MemoryFailureLogEntry, ...]:
        """Return bounded, sanitized terminal failure history."""

        return await self._store_call(self._store.failure_log, limit=limit)

    async def clear(self) -> ClearReceipt:
        """Run one idempotent, bounded clear lifecycle operation."""

        async with self._lifecycle_lock:
            async with self._root_lifecycle_lock():
                self._clear_active = True
                try:
                    started = await self._store_call(self._store.begin_clear)
                    if not await self._worker.pause_and_wait(
                        timeout_seconds=self._clear_drain_timeout_seconds
                    ):
                        raise _ClearStepFailure("worker drain did not stop in time")
                    await self._clear_provider_data_or_fail(started)
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
            async with self._root_lifecycle_lock():
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
                    await self._clear_provider_data_or_fail(meta)
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
            "syncing",
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
            awaiting_receipt=stats.awaiting_receipt,
            succeeded=stats.succeeded,
            receipt_unknown=stats.receipt_unknown,
            distill_failed=stats.distill_failed,
            dead=stats.dead,
            missed=meta.missed_count if meta is not None else 0,
            queue_plaintext_bytes=stats.queue_plaintext_bytes,
            provider_disk_bytes=await asyncio.to_thread(self._provider_disk_bytes),
            last_success_at=meta.last_success_at if meta is not None else None,
            last_flush_observation=stats.last_flush_observation,
            last_flush_status=stats.last_flush_status,
            last_flush_error_code=stats.last_flush_error_code,
            last_flush_request_id=stats.last_flush_request_id,
            last_flush_at=stats.last_flush_at,
            processing_fault_kind=meta.processing_fault_kind if meta is not None else None,
            processing_fault_since=meta.processing_fault_since if meta is not None else None,
            processing_alert_active=meta.processing_alert_active if meta is not None else False,
            error=error,
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

    async def _has_minimum_free_disk(self) -> bool:
        try:
            return int(await asyncio.to_thread(self._disk_free_bytes)) >= MIN_FREE_DISK_BYTES
        except Exception:
            return False

    async def _clear_provider_data_or_fail(self, meta: MemoryMeta) -> None:
        """Clear one verified root without allowing timed-out cleanup to escape ownership."""

        await asyncio.to_thread(self._verify_owned_provider_root, meta, require_empty=False)
        await self._run_owned_provider_cleanup()
        await asyncio.to_thread(self._recreate_owned_provider_root, meta)

    def _ensure_owned_provider_root(self, meta: MemoryMeta) -> None:
        """Create the first sentinel-owned root or verify an existing one.

        Runtime wiring calls this private helper before starting EverOS. Keeping
        it here means first enablement and Clear all use the same ownership
        sentinel rules without widening the frozen MemoryModule interface.
        """

        _ensure_provider_root_chain_safe(self._provider_root, self._effective_home)
        parent = self._provider_root.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise _ClearStepFailure("provider root parent cannot be created") from error
        _ensure_provider_root_chain_safe(self._provider_root, self._effective_home)
        parent_info = _lstat_or_clear_failure(parent, "provider root parent")
        _require_owned_directory(parent_info, "provider root parent", private=True)
        try:
            root_info = self._provider_root.lstat()
        except FileNotFoundError:
            self._provider_root.mkdir(mode=0o700)
            root_info = self._provider_root.lstat()
        _require_owned_directory(root_info, "provider root", private=True)
        sentinel = self._provider_root / ROOT_SENTINEL_FILENAME
        if sentinel.exists() or sentinel.is_symlink():
            self._verify_owned_provider_root(meta, require_empty=False)
            return
        try:
            with os.scandir(self._provider_root) as entries:
                if any(True for _entry in entries):
                    raise _ClearStepFailure("provider root is not empty")
        except OSError as error:
            raise _ClearStepFailure("provider root cannot be read") from error
        self._write_root_sentinel(meta)

    def _root_lifecycle_lock(self) -> asyncio.Lock:
        return _ROOT_LIFECYCLE_LOCKS.setdefault(self._provider_root_key, asyncio.Lock())

    async def _run_owned_provider_cleanup(self) -> None:
        """Await a cleanup task once, retaining it after timeout until it actually ends."""

        existing = _ROOT_CLEANUP_TASKS.get(self._provider_root_key)
        if existing is not None:
            if not existing.done():
                raise _ClearStepFailure("provider cleanup is still running")
            _ROOT_CLEANUP_TASKS.pop(self._provider_root_key, None)
            try:
                existing.result()
            except BaseException as error:
                raise _ClearStepFailure("provider cleanup failed") from error
            return

        task = asyncio.create_task(self._invoke_provider_cleanup())
        _ROOT_CLEANUP_TASKS[self._provider_root_key] = task
        task.add_done_callback(_consume_cleanup_task_exception)
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._clear_cleanup_timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            # Shielding leaves the task owned here.  A later recovery sees it and
            # cannot start a second cleanup against the same provider root.
            raise _ClearStepFailure("provider clear timed out") from error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _ROOT_CLEANUP_TASKS.pop(self._provider_root_key, None)
            raise _ClearStepFailure("provider cleanup failed") from error
        else:
            _ROOT_CLEANUP_TASKS.pop(self._provider_root_key, None)

    async def _invoke_provider_cleanup(self) -> None:
        callback = self._clear_provider_data
        if callback is None:
            raise _ClearStepFailure("provider clear dependency is unavailable")
        result = await asyncio.to_thread(callback)
        if inspect.isawaitable(result):
            await result

    def _verify_owned_provider_root(self, meta: MemoryMeta, *, require_empty: bool) -> None:
        _ensure_provider_root_chain_safe(self._provider_root, self._effective_home)
        root_info = _lstat_or_clear_failure(self._provider_root, "provider root")
        _require_owned_directory(root_info, "provider root", private=True)
        sentinel_path = self._provider_root / ROOT_SENTINEL_FILENAME
        sentinel_info = _lstat_or_clear_failure(sentinel_path, "provider root sentinel")
        _require_owned_regular_file(sentinel_info, "provider root sentinel", private=True)
        sentinel = _read_root_sentinel(sentinel_path)
        expected_keys = {
            "schema_version",
            "provider_root_id",
            "provider_id",
            "provider_root_format",
            "created_by_artifact_fingerprint",
        }
        if not isinstance(sentinel, dict) or set(sentinel) != expected_keys:
            raise _ClearStepFailure("provider root sentinel is invalid")
        if (
            type(sentinel.get("schema_version")) is not int
            or sentinel.get("schema_version") != ROOT_SENTINEL_SCHEMA_VERSION
        ):
            raise _ClearStepFailure("provider root sentinel schema is invalid")
        if sentinel.get("provider_root_id") != meta.provider_root_id:
            raise _ClearStepFailure("provider root id does not match")
        if sentinel.get("provider_id") != ROOT_PROVIDER_ID:
            raise _ClearStepFailure("provider root owner does not match")
        if sentinel.get("provider_root_format") not in self._compatible_provider_root_formats:
            raise _ClearStepFailure("provider root format does not match")
        if not _is_root_metadata_value(sentinel.get("created_by_artifact_fingerprint")):
            raise _ClearStepFailure("provider root sentinel is invalid")

        if require_empty:
            try:
                with os.scandir(self._provider_root) as entries:
                    if any(entry.name != ROOT_SENTINEL_FILENAME for entry in entries):
                        raise _ClearStepFailure("provider root still contains data")
            except OSError as error:
                raise _ClearStepFailure("provider root cannot be read") from error

    def _recreate_owned_provider_root(self, meta: MemoryMeta) -> None:
        """Remove all provider children with no-follow traversal, preserving the root itself."""

        # The sentinel remains until the replacement is atomically installed, so
        # a crash retains a verifiable root for idempotent recovery.
        self._verify_owned_provider_root(meta, require_empty=False)
        try:
            with os.scandir(self._provider_root) as entries:
                children = [Path(entry.path) for entry in entries if entry.name != ROOT_SENTINEL_FILENAME]
        except OSError as error:
            raise _ClearStepFailure("provider root cannot be read") from error
        for child in children:
            _remove_root_child_no_follow(child)
        self._write_root_sentinel(meta)
        self._verify_owned_provider_root(meta, require_empty=True)

    def _write_root_sentinel(self, meta: MemoryMeta) -> None:
        payload = json.dumps(
            {
                "schema_version": ROOT_SENTINEL_SCHEMA_VERSION,
                "provider_root_id": meta.provider_root_id,
                "provider_id": ROOT_PROVIDER_ID,
                "provider_root_format": self._provider_root_format,
                "created_by_artifact_fingerprint": self._artifact_fingerprint,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary = self._provider_root / f".{ROOT_SENTINEL_FILENAME}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self._provider_root / ROOT_SENTINEL_FILENAME)
            sentinel_info = _lstat_or_clear_failure(
                self._provider_root / ROOT_SENTINEL_FILENAME,
                "provider root sentinel",
            )
            _require_owned_regular_file(sentinel_info, "provider root sentinel", private=True)
        except OSError as error:
            raise _ClearStepFailure("provider root sentinel could not be written") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass

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


def _consume_cleanup_task_exception(task: asyncio.Task[None]) -> None:
    """Retrieve a retained task error without exposing provider details anywhere."""

    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


def _lstat_or_clear_failure(path: Path, label: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as error:
        raise _ClearStepFailure(f"{label} is unavailable") from error


def _require_owned_directory(info: os.stat_result, label: str, *, private: bool) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _ClearStepFailure(f"{label} is not an owned directory")
    _require_current_user_owner(info, label)
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise _ClearStepFailure(f"{label} is not owner-only")


def _require_owned_regular_file(info: os.stat_result, label: str, *, private: bool) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _ClearStepFailure(f"{label} is not an owned regular file")
    _require_current_user_owner(info, label)
    if private and stat.S_IMODE(info.st_mode) != 0o600:
        raise _ClearStepFailure(f"{label} is not owner-only")


def _require_current_user_owner(info: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and info.st_uid != getuid():
        raise _ClearStepFailure(f"{label} has an unexpected owner")


def _read_root_sentinel(path: Path) -> object:
    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags | no_follow)
        _require_owned_regular_file(
            os.fstat(descriptor),
            "provider root sentinel",
            private=True,
        )
        chunks: list[bytes] = []
        remaining = MAX_ROOT_SENTINEL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as error:
        raise _ClearStepFailure("provider root sentinel cannot be read") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_ROOT_SENTINEL_BYTES:
        raise _ClearStepFailure("provider root sentinel is too large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise _ClearStepFailure("provider root sentinel is invalid") from error


def _remove_root_child_no_follow(path: Path) -> None:
    try:
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            for child in children:
                _remove_root_child_no_follow(child)
            os.rmdir(path)
        else:
            os.unlink(path)
    except OSError as error:
        raise _ClearStepFailure("provider root child could not be removed") from error


def _ensure_provider_root_chain_safe(provider_root: Path, effective_home: Path) -> None:
    """Reject a provider root whose path reaches its target via a symlinked component.

    The final root and sentinel are validated separately; this guards every PARENT
    component so that clear/delete cannot traverse a symlinked directory and remove
    data outside the intended root (tech §13 exact-root/no-follow requirement).
    Each component from the root upward is lstat'd (no follow) until it reaches the
    effective home or the filesystem root; a symlink anywhere on that chain is
    rejected. Components below the effective home (e.g. an isolated test tmpdir) are
    still checked for symlinks but are not required to live inside the home.
    """
    home_abs = Path(os.path.abspath(os.fspath(effective_home)))
    current = Path(os.path.abspath(os.fspath(provider_root)))
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # A not-yet-created ancestor is acceptable (clear recreates the chain);
            # only existing components are checked for symlink escape.
            pass
        else:
            if stat.S_ISLNK(info.st_mode):
                raise _ClearStepFailure("provider root chain contains a symlink")
        if current == current.parent:
            break
        if current == home_abs:
            break
        current = current.parent


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        result = os.write(descriptor, payload[written:])
        if result <= 0:
            raise OSError("provider root sentinel write failed")
        written += result


def _is_root_metadata_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character in {"-", "_", "."} for character in value)
    )


def _root_metadata_value(value: object, *, fallback: str) -> str:
    return value if _is_root_metadata_value(value) else fallback


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
