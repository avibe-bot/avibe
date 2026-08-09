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
import weakref
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from config import paths
from core.memory.blocking import run_blocking
from core.memory.attachments import (
    AttachmentPinError,
    AttachmentPinStore,
    PinnedBundle,
    encode_pinned_bundle,
)
from core.memory.artifact import PROVIDER_ROOT_CONTROL_FILES
from core.memory.confined_filesystem import (
    ConfinedFilesystemError,
    remove_confined_path,
)
from core.memory.everos import MemoryProviderFailure, MemoryProviderPort
from core.memory.store import (
    MAX_NONTERMINAL_QUEUE_ROWS,
    MemoryMeta,
    MemoryStore,
    is_principal_id,
    is_project_id,
)
from core.memory.types import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureReceipt,
    CaptureRequest,
    CaptureSkipped,
    MemoryErrorCode,
    MemoryFailureLogEntry,
    MemoryItem,
    MemoryItems,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    MemoryResult,
    OperationFailed,
    RecallItems,
    RecallPolicy,
    RecallResult,
    is_memory_error_code,
)
from core.memory.worker import MemoryWorker, ProcessingEvent


MAX_CAPTURE_TEXT_BYTES = 32 * 1024
MAX_CAPTURE_IDENTIFIER_BYTES = 1024
MAX_CAPTURE_ATTACHMENTS = 8
MAX_CAPTURE_ATTACHMENT_METADATA_BYTES = 16 * 1024
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
        maintenance_open: Callable[[], bool] | None = None,
        provider_root_format: str = SLICE1_PROVIDER_ROOT_FORMAT,
        artifact_fingerprint: str = SLICE1_ARTIFACT_FINGERPRINT,
        compatible_provider_root_formats: Iterable[str] = (),
        clear_drain_timeout_seconds: float = CLEAR_DRAIN_TIMEOUT_SECONDS,
        clear_cleanup_timeout_seconds: float = CLEAR_CLEANUP_TIMEOUT_SECONDS,
        processing_event: ProcessingEvent | None = None,
        worker: MemoryWorker | None = None,
        attachment_store: AttachmentPinStore | None = None,
        effective_home: Path | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled_source = enabled
        self._runtime_error_source = runtime_error
        self._starting_source = starting
        self._disk_free_bytes = disk_free_bytes or self._default_free_disk_bytes
        self._effective_home = (
            paths.get_vibe_remote_dir()
            if effective_home is None
            else effective_home
        )
        self._provider_root = provider_root or (self._effective_home / "memory" / "everos-root")
        self._provider_root_key = os.path.abspath(os.fspath(self._provider_root))
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
        self._maintenance_open = maintenance_open or (lambda: False)
        self._clear_drain_timeout_seconds = _positive_timeout(clear_drain_timeout_seconds)
        self._clear_cleanup_timeout_seconds = _positive_timeout(clear_cleanup_timeout_seconds)
        self._lifecycle_lock = asyncio.Lock()
        self._capture_admission_locks: weakref.WeakValueDictionary[
            tuple[str, str, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._invalid_capture_admission_lock = asyncio.Lock()
        self._clear_active = False
        self._attachment_store = attachment_store or AttachmentPinStore(
            effective_home=self._effective_home
        )
        self._worker = worker or MemoryWorker(
            store=store,
            provider=provider,
            enabled=self._is_enabled,
            processing_event=processing_event,
            attachment_store=self._attachment_store,
            attachment_admission_lock=self._root_lifecycle_lock(),
        )

    def _replace_provider(self, provider: MemoryProviderPort) -> None:
        """Swap the private provider shared by direct reads and the worker.

        ``MemoryRuntime`` holds the module lifecycle lock before invoking this,
        so a sidecar credential/runtime replacement cannot split these two
        consumers across provider instances.
        """

        self._provider = provider
        self._worker.replace_provider(provider)

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
        self._verify_owned_provider_root(meta, require_empty=True, allow_format_mismatch=True)
        sentinel = _read_root_sentinel(self._provider_root / ROOT_SENTINEL_FILENAME)
        current_format = sentinel.get("provider_root_format") if isinstance(sentinel, dict) else None
        if current_format == self._provider_root_format:
            return False
        previous_fingerprint = sentinel.get("created_by_artifact_fingerprint")
        try:
            self._write_root_sentinel(meta)
            self._verify_owned_provider_root(meta, require_empty=True)
        except Exception:
            self._write_root_sentinel(
                meta,
                provider_root_format=current_format,
                artifact_fingerprint=previous_fingerprint,
            )
            self._verify_owned_provider_root(
                meta,
                require_empty=True,
                allow_format_mismatch=True,
            )
            raise
        return True

    async def capture(self, request: CaptureRequest) -> CaptureReceipt:
        """Validate and persist one source capture without touching the provider."""

        if not self._is_enabled():
            return CaptureSkipped(reason="memory_disabled")
        if self._clear_active or self._is_maintenance_open():
            return CaptureSkipped(reason="memory_clear_failed")

        admission_lock = (
            self._capture_admission_lock(
                principal_id=request.principal_id,
                project_id=request.project_id,
                session_id=request.session_id,
            )
            if isinstance(request, CaptureRequest)
            else self._invalid_capture_admission_lock
        )
        async with admission_lock:
            async with self._root_lifecycle_lock():
                if not self._is_enabled():
                    return CaptureSkipped(reason="memory_disabled")
                if self._clear_active or self._is_maintenance_open():
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
                return await self._capture_under_root(request, normalized_text)

    async def _capture_under_root(
        self,
        request: CaptureRequest,
        normalized_text: str,
    ) -> CaptureReceipt:
        """Pin and enqueue one validated capture under the provider-root fence."""

        pinned_bundle: PinnedBundle | None = None
        try:
            if request.attachments:
                pinned_bundle = await run_blocking(
                    self._attachment_store.pin,
                    request.attachments,
                )
            attachment_payload = (
                encode_pinned_bundle(pinned_bundle)
                if pinned_bundle is not None
                else None
            )
            result = await self._store_call(
                self._store.enqueue_request,
                source_message_id=request.source_message_id,
                session_id=request.session_id,
                principal_id=request.principal_id,
                project_ref=request.project_id,
                provenance=request.provenance,
                payload_text=normalized_text,
                payload_attachments=attachment_payload,
                attachment_bundle_id=(
                    pinned_bundle.bundle_id if pinned_bundle is not None else None
                ),
                attachment_bundle_relative_path=(
                    pinned_bundle.relative_path if pinned_bundle is not None else None
                ),
                attachment_file_count=(
                    len(pinned_bundle.attachments) if pinned_bundle is not None else 0
                ),
                attachment_total_bytes=(
                    pinned_bundle.total_bytes if pinned_bundle is not None else 0
                ),
                occurred_at_ms=request.occurred_at_ms,
                max_provider_timestamp_ms=MAX_PROVIDER_TIMESTAMP_MS,
                nonterminal_limit=MAX_NONTERMINAL_QUEUE_ROWS,
            )
        except AttachmentPinError as error:
            return await self._capture_pin_failure(error.error)
        except UnicodeError:
            if pinned_bundle is not None:
                await self._release_unadmitted_bundle(pinned_bundle.bundle_id)
            return await self._skipped_with_missed("memory_invalid_input")
        except Exception:
            if pinned_bundle is not None:
                await self._release_unadmitted_bundle(pinned_bundle.bundle_id)
            return OperationFailed(error="memory_store_unavailable")

        if result.outcome == "accepted":
            return CaptureAccepted()
        if pinned_bundle is not None:
            await self._release_unadmitted_bundle(pinned_bundle.bundle_id)
        if result.outcome == "duplicate":
            return CaptureDuplicate()
        if result.outcome == "queue_full":
            return CaptureSkipped(reason="memory_queue_full")
        if result.outcome == "timestamp_invalid":
            return CaptureSkipped(reason="memory_invalid_input")
        return CaptureSkipped(reason="memory_clear_failed")

    async def _capture_pin_failure(self, error: MemoryErrorCode) -> CaptureReceipt:
        if error == "memory_store_unavailable":
            return OperationFailed(error=error)
        if error in {
            "memory_invalid_input",
            "memory_input_too_large",
            "memory_low_disk_space",
        }:
            return await self._skipped_with_missed(error)
        return OperationFailed(error="memory_store_unavailable")

    async def _release_unadmitted_bundle(self, bundle_id: str) -> None:
        try:
            await run_blocking(self._attachment_store.release, bundle_id)
        except Exception:
            # It has no DB reference and boot reconciliation removes the orphan.
            return

    async def search(
        self,
        query: str,
        *,
        principal_id: str,
        project_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> MemoryResult:
        """Compatibility wrapper for the default one-run hybrid recall policy."""

        try:
            policy = RecallPolicy(mode="hybrid", max_results=limit)
        except ValueError:
            return OperationFailed(error="memory_invalid_input")
        result = await self.recall(
            query,
            policy=policy,
            principal_id=principal_id,
            project_id=project_id,
        )
        if isinstance(result, RecallItems):
            return MemoryItems(items=result.items, warnings=result.warnings)
        return result

    async def recall(
        self,
        query: str,
        *,
        policy: RecallPolicy,
        principal_id: str,
        project_id: str,
        current_session_id: str | None = None,
    ) -> RecallResult:
        """Execute one capability-gated recall decision and at most one search."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        if not isinstance(policy, RecallPolicy):
            return OperationFailed(error="memory_invalid_input")
        normalized_query = self._normalize_text(query)
        query_bytes = _utf8_bytes(normalized_query)
        if query_bytes is None or not normalized_query.strip():
            return OperationFailed(error="memory_invalid_input")
        if not is_principal_id(principal_id):
            return OperationFailed(error="memory_access_denied")
        if not is_project_id(project_id):
            return OperationFailed(error="memory_access_denied")
        if len(query_bytes) > MAX_QUERY_BYTES:
            return OperationFailed(error="memory_input_too_large")

        if self._clear_active or self._is_maintenance_open():
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
            session_ref = None
            if policy.include_current_session:
                if not isinstance(current_session_id, str) or not current_session_id.strip():
                    return OperationFailed(error="memory_invalid_input")
                try:
                    session_ref = await self._store_call(
                        self._store.provider_session_ref,
                        principal_id=principal_id,
                        project_ref=project_id,
                        session_id=current_session_id.strip(),
                    )
                except ValueError:
                    return OperationFailed(error="memory_access_denied")
                except Exception:
                    return OperationFailed(error="memory_store_unavailable")

            requested_mode = policy.mode
            if requested_mode == "agentic":
                # EverOS 1.2.3 has no public model-call/token budget contract.
                # A local timeout alone cannot make this policy enforceable.
                if not bool(getattr(self._provider, "agentic_budget_enforced", False)):
                    return OperationFailed(error="memory_capability_unavailable")
                effective_mode: Literal["keyword", "vector", "hybrid", "agentic"] = "agentic"
            elif requested_mode == "keyword":
                effective_mode = "keyword"
            else:
                try:
                    health = await asyncio.wait_for(
                        self._provider.health_snapshot(),
                        timeout=PROVIDER_READ_TIMEOUT_SECONDS,
                    )
                    embed_available = health.capabilities.get("embed") is True
                except Exception:
                    embed_available = False
                if requested_mode == "auto":
                    effective_mode = "hybrid" if embed_available else "keyword"
                elif not embed_available:
                    return OperationFailed(error="memory_capability_unavailable")
                else:
                    effective_mode = requested_mode
            result = await self._provider_read(
                lambda: self._provider.search(
                    principal_id,
                    project_id,
                    normalized_query,
                    policy.max_results,
                    method=effective_mode,
                    include_profile=policy.include_profile,
                    session_ref=session_ref,
                )
            )
        if isinstance(result, OperationFailed):
            return result
        bounded = self._bounded_items(result, limit=policy.max_results)
        if isinstance(bounded, OperationFailed):
            return bounded
        return RecallItems(
            items=bounded.items,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            current_session_overlay=session_ref is not None,
        )

    async def profile(self, *, principal_id: str, project_id: str) -> MemoryResult:
        """Return a bounded provider profile result or one closed error category."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        if not is_principal_id(principal_id):
            return OperationFailed(error="memory_access_denied")
        if not is_project_id(project_id):
            return OperationFailed(error="memory_access_denied")
        if self._clear_active or self._is_maintenance_open():
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
            result = await self._provider_read(lambda: self._provider.profile(principal_id, project_id))
        return result if isinstance(result, OperationFailed) else self._bounded_items(
            result,
            limit=MAX_PROVIDER_RESULT_ITEMS,
        )

    async def failure_log(self, *, limit: int = 50) -> tuple[MemoryFailureLogEntry, ...]:
        """Return terminal failure history while fencing its bounded compaction."""

        if self._clear_active or self._is_maintenance_open():
            return ()
        async with self._root_lifecycle_lock():
            if self._clear_active or self._is_maintenance_open():
                return ()
            return await self._store_call(self._store.failure_log, limit=limit)

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
            if item.profile is not None:
                if item.kind != "profile":
                    return OperationFailed(error="memory_provider_response_invalid")
                profile_bytes = _profile_bytes(item.profile)
                if profile_bytes is None:
                    return OperationFailed(error="memory_provider_response_invalid")
                total_bytes += profile_bytes
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
        if (
            not is_principal_id(request.principal_id)
            or not is_project_id(request.project_id)
            or request.provenance not in {"user_input", "agent"}
        ):
            return "memory_invalid_input"
        if not isinstance(request.occurred_at_ms, int) or isinstance(request.occurred_at_ms, bool):
            return "memory_invalid_input"
        if request.occurred_at_ms < 0 or request.occurred_at_ms > MAX_PROVIDER_TIMESTAMP_MS:
            return "memory_invalid_input"
        if (
            not isinstance(request.attachments, tuple)
            or len(request.attachments) > MAX_CAPTURE_ATTACHMENTS
            or any(not self._valid_capture_attachment(item) for item in request.attachments)
        ):
            return "memory_invalid_input"
        attachment_metadata = "\0".join(
            f"{item.kind}\0{item.name}\0{item.ext}"
            for item in request.attachments
        )
        attachment_bytes = _utf8_bytes(attachment_metadata)
        if attachment_bytes is None:
            return "memory_invalid_input"
        if len(attachment_bytes) > MAX_CAPTURE_ATTACHMENT_METADATA_BYTES:
            return "memory_input_too_large"
        text_bytes = _utf8_bytes(normalized_text)
        if text_bytes is None:
            return "memory_invalid_input"
        if not normalized_text.strip() and not request.attachments:
            return "memory_invalid_input"
        if len(text_bytes) > MAX_CAPTURE_TEXT_BYTES:
            return "memory_input_too_large"
        return None

    @staticmethod
    def _valid_capture_attachment(value: object) -> bool:
        if not isinstance(value, CaptureAttachment):
            return False
        if value.kind not in {"image", "audio", "doc", "pdf", "html", "email"}:
            return False
        if not all(isinstance(item, str) and item for item in (value.name, value.uri, value.ext)):
            return False
        if not value.uri.startswith("file://") or not value.ext.isalnum() or len(value.ext) > 8:
            return False
        return _utf8_bytes("\0".join((value.name, value.uri, value.ext))) is not None

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

    def _is_maintenance_open(self) -> bool:
        try:
            return bool(self._maintenance_open())
        except Exception:
            return True

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

    def _capture_admission_lock(
        self,
        *,
        principal_id: object,
        project_id: object,
        session_id: object,
    ) -> asyncio.Lock:
        """Return the exact-session fence covering pin through queue commit."""

        if not all(
            isinstance(value, str)
            for value in (principal_id, project_id, session_id)
        ):
            return self._invalid_capture_admission_lock
        key = (principal_id, project_id, session_id)
        lock = self._capture_admission_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._capture_admission_locks[key] = lock
        return lock

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

    def _verify_owned_provider_root(
        self,
        meta: MemoryMeta,
        *,
        require_empty: bool,
        allow_format_mismatch: bool = False,
    ) -> None:
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
        if (
            not allow_format_mismatch
            and sentinel.get("provider_root_format") not in self._compatible_provider_root_formats
        ):
            raise _ClearStepFailure("provider root format does not match")
        if not _is_root_metadata_value(sentinel.get("created_by_artifact_fingerprint")):
            raise _ClearStepFailure("provider root sentinel is invalid")

        if require_empty:
            try:
                with os.scandir(self._provider_root) as entries:
                    if any(entry.name not in PROVIDER_ROOT_CONTROL_FILES for entry in entries):
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
            _remove_root_child_no_follow(child, self._effective_home)
        self._write_root_sentinel(meta)
        self._verify_owned_provider_root(meta, require_empty=True)

    def _write_root_sentinel(
        self,
        meta: MemoryMeta,
        *,
        provider_root_format: str | None = None,
        artifact_fingerprint: str | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "schema_version": ROOT_SENTINEL_SCHEMA_VERSION,
                "provider_root_id": meta.provider_root_id,
                "provider_id": ROOT_PROVIDER_ID,
                "provider_root_format": provider_root_format or self._provider_root_format,
                "created_by_artifact_fingerprint": artifact_fingerprint or self._artifact_fingerprint,
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
        return await run_blocking(method, *args, **kwargs)

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


def _profile_text_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    encoded = _utf8_bytes(value)
    if encoded is None or len(encoded) > MAX_PROVIDER_ITEM_BYTES:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t", "\r"} for character in value):
        return None
    return encoded


def _profile_bytes(profile: object) -> int | None:
    """Revalidate every structured profile field before it leaves the module."""

    if not isinstance(profile, MemoryProfile):
        return None
    if profile.summary is None and not profile.explicit_info and not profile.implicit_traits:
        return None

    total = 0

    def optional_text(value: object) -> int | None:
        if value is None:
            return 0
        encoded = _profile_text_bytes(value)
        return len(encoded) if encoded is not None else None

    summary_bytes = optional_text(profile.summary)
    if summary_bytes is None:
        return None
    total += summary_bytes

    if not isinstance(profile.explicit_info, tuple) or len(profile.explicit_info) > MAX_PROVIDER_RESULT_ITEMS * 10:
        return None
    for info in profile.explicit_info:
        if not isinstance(info, MemoryProfileExplicitInfo):
            return None
        description_bytes = _profile_text_bytes(info.description)
        if description_bytes is None:
            return None
        total += len(description_bytes)
        for value in (info.category, info.evidence):
            value_bytes = optional_text(value)
            if value_bytes is None:
                return None
            total += value_bytes

    if not isinstance(profile.implicit_traits, tuple) or len(profile.implicit_traits) > MAX_PROVIDER_RESULT_ITEMS * 10:
        return None
    for trait in profile.implicit_traits:
        if not isinstance(trait, MemoryProfileTrait):
            return None
        description_bytes = _profile_text_bytes(trait.description)
        if description_bytes is None:
            return None
        total += len(description_bytes)
        for value in (trait.trait, trait.basis, trait.evidence):
            value_bytes = optional_text(value)
            if value_bytes is None:
                return None
            total += value_bytes

    if profile.updated_at is not None:
        timestamp_bytes = _profile_text_bytes(profile.updated_at)
        if timestamp_bytes is None or len(timestamp_bytes) > 64:
            return None
        try:
            instant = datetime.fromisoformat(profile.updated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant):
            return None
        total += len(timestamp_bytes)

    return total


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


def _remove_root_child_no_follow(path: Path, effective_home: Path) -> None:
    try:
        remove_confined_path(effective_home, path)
    except (ConfinedFilesystemError, OSError, ValueError) as error:
        raise _ClearStepFailure("provider root child could not be removed") from error


def _ensure_provider_root_chain_safe(provider_root: Path, effective_home: Path) -> None:
    """Reject a provider root whose path reaches its target via a symlinked component.

    The final root and sentinel are validated separately; this guards every PARENT
    component so that clear/delete cannot traverse a symlinked directory and remove
    data outside the intended root (the exact-root/no-follow requirement).
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
