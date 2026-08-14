"""The provider-independent MemoryModule interface."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import unicodedata
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, AsyncIterator, Literal, TypeVar

from config import paths
from core.memory.blocking import run_blocking
from core.memory.attachments import (
    AttachmentPinError,
    AttachmentPinStore,
    PinnedBundle,
    encode_pinned_bundle,
)
from core.memory.provider_root import ProviderRoot
from core.memory.everos import (
    AgenticRecallTelemetry,
    MemoryProviderFailure,
    MemoryProviderPort,
)
from core.memory.project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
    is_writable_memory_project_id,
)
from core.memory.store import (
    MAX_NONTERMINAL_QUEUE_ROWS,
    MemoryStore,
    is_principal_id,
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
    MemoryListItem,
    MemoryListPage,
    MemoryListResult,
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
MAX_LIST_PAGE_SIZE = 20
DEFAULT_SEARCH_LIMIT = 8
MAX_PROVIDER_ITEM_BYTES = 64 * 1024
MAX_PROVIDER_RESULT_BYTES = 256 * 1024
MAX_PROVIDER_RESULT_ITEMS = 20
PROVIDER_READ_TIMEOUT_SECONDS = 20.0
CLEAR_DRAIN_TIMEOUT_SECONDS = 5.0


logger = logging.getLogger(__name__)
_SessionLifecycleResult = TypeVar("_SessionLifecycleResult")


_ROOT_LIFECYCLE_LOCKS: dict[str, asyncio.Lock] = {}
_RFC3339_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)


class MemorySessionLifecycleBusyError(RuntimeError):
    """Raised when a destructive session transition cannot acquire its fence."""

    code = "memory_session_lifecycle_busy"


class MemoryModule:
    """Own local capture, direct reads, status, and clear without exposing internals."""

    def __init__(
        self,
        store: MemoryStore,
        provider: MemoryProviderPort,
        *,
        enabled: bool | Callable[[], bool] = False,
        disk_free_bytes: Callable[[], int] | None = None,
        provider_root: Path | None = None,
        maintenance_open: Callable[[], bool] | None = None,
        provider_root_owner: ProviderRoot | None = None,
        clear_drain_timeout_seconds: float = CLEAR_DRAIN_TIMEOUT_SECONDS,
        processing_event: ProcessingEvent | None = None,
        worker: MemoryWorker | None = None,
        attachment_store: AttachmentPinStore | None = None,
        effective_home: Path | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._enabled_source = enabled
        self._disk_free_bytes = disk_free_bytes or self._default_free_disk_bytes
        self._effective_home = (
            paths.get_vibe_remote_dir()
            if effective_home is None
            else effective_home
        )
        self._provider_root = provider_root or (self._effective_home / "memory" / "everos-root")
        self._provider_root_key = os.path.abspath(os.fspath(self._provider_root))
        self.provider_root = provider_root_owner or ProviderRoot(
            self._provider_root,
            effective_home=self._effective_home,
        )
        self._maintenance_open = maintenance_open or (lambda: False)
        self._clear_drain_timeout_seconds = _positive_timeout(clear_drain_timeout_seconds)
        self._lifecycle_lock = asyncio.Lock()
        self._capture_admission_locks: weakref.WeakValueDictionary[
            tuple[str, str, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._invalid_capture_admission_lock = asyncio.Lock()
        self._clear_active = False
        self._retired = False
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

    @property
    def maintenance_active(self) -> bool:
        """Whether Runtime-owned maintenance currently fences module work."""

        return self._clear_active

    @property
    def retired(self) -> bool:
        """Whether this captured module has been permanently tombstoned."""

        return self._retired

    def retire(self) -> None:
        """Permanently close this module to stale callers before root deletion."""

        self._retired = True
        self._clear_active = True
        self._worker.pause_claims()

    def enter_maintenance(self) -> None:
        """Fence capture and reads before a maintenance transition begins."""

        self._clear_active = True

    def leave_maintenance(self) -> None:
        """Reopen capture and reads after maintenance ownership is released."""

        self._clear_active = False

    @asynccontextmanager
    async def lifecycle(self) -> AsyncIterator[None]:
        """Serialize an ordinary module lifecycle transition."""

        async with self._lifecycle_lock:
            yield

    @asynccontextmanager
    async def destructive_lifecycle(self) -> AsyncIterator[None]:
        """Acquire module and provider-root ownership in their required order."""

        async with self._lifecycle_lock:
            async with self._root_lifecycle_lock():
                yield

    @asynccontextmanager
    async def provider_root_lifecycle(self) -> AsyncIterator[None]:
        """Fence provider-root work when module lifecycle ownership is already held."""

        async with self._root_lifecycle_lock():
            yield

    @asynccontextmanager
    async def observe_provider_root(self) -> AsyncIterator[bool]:
        """Acquire provider-root observation only when it is immediately available."""

        lock = self._root_lifecycle_lock()
        if lock.locked():
            yield False
            return
        # An uncontended asyncio.Lock acquisition completes without yielding.
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    def pause_claims(self) -> None:
        """Synchronously fence new add and flush claims."""

        self._worker.pause_claims()

    async def quiesce_claims(self, *, timeout_seconds: float | None = None) -> bool:
        """Fence claims and join in-flight add and flush work under one deadline."""

        if timeout_seconds is None:
            return await self._worker.pause_and_wait()
        return await self._worker.pause_and_wait(timeout_seconds=timeout_seconds)

    async def quiesce_claims_for_clear(self) -> bool:
        """Fence claims using the module's configured destructive-work budget."""

        return await self._worker.pause_and_wait(
            timeout_seconds=self._clear_drain_timeout_seconds
        )

    def resume_claims(self) -> None:
        """Permit add and flush claims after lifecycle recovery succeeds."""

        self._worker.resume_claims()

    def begin_activation(self, *, new_lease: bool = False) -> None:
        """Require recovery before the next drain, optionally rotating lease ownership."""

        if new_lease:
            self._worker.begin_new_lease_activation()
            return
        self._worker.begin_activation()

    async def drain(self) -> int:
        """Run one bounded worker drain through the module interface."""

        return await self._worker.drain()

    async def prepare_shutdown(self) -> None:
        """Settle in-process flush work without initiating provider writes."""

        await self._worker.prepare_shutdown()

    async def clear_attachments(self) -> None:
        """Remove every module-owned pinned attachment during destructive clear."""

        await run_blocking(self._attachment_store.clear_all)

    async def final_flush(
        self,
        *,
        principal_id: str,
        project_id: str,
        raw_session_id: str,
        deadline_seconds: float = 5.0,
    ) -> bool:
        """Fence capture and flush one trusted canonical session by deadline."""

        if not self._is_enabled() or self.maintenance_active or self._is_maintenance_open():
            return False
        timeout = _positive_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        admission_lock = self._capture_admission_lock(
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

        if not self._is_enabled() or self.maintenance_active or self._is_maintenance_open():
            return await operation()
        timeout = _positive_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        admission_lock = self._capture_admission_lock(
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

        canonical_scopes = tuple(dict.fromkeys(scopes))
        if (
            not canonical_scopes
            or not isinstance(raw_session_id, str)
            or not raw_session_id
            or any(
                not is_principal_id(principal_id) or not is_persisted_memory_project_id(project_id)
                for principal_id, project_id in canonical_scopes
            )
        ):
            raise ValueError("invalid canonical Memory session scopes")
        if not self._is_enabled():
            return await operation()
        if self.maintenance_active or self._is_maintenance_open():
            raise MemorySessionLifecycleBusyError("memory session lifecycle is unavailable")

        timeout = _positive_timeout(deadline_seconds)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        locks = [
            self._capture_admission_lock(
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
            flush_succeeded = True
            for principal_id, project_id in canonical_scopes:
                flush_succeeded = (
                    await self._final_flush_under_admission(
                    principal_id=principal_id,
                    project_id=project_id,
                    raw_session_id=raw_session_id,
                    deadline=deadline,
                    )
                    and flush_succeeded
                )
            result = await operation()
            if isinstance(result, bool):
                return result and flush_succeeded
            return result
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
        if not self._is_enabled() or self.maintenance_active or self._is_maintenance_open():
            return False
        try:
            session_ref = await self._store_call(
                self._store.provider_session_ref,
                principal_id=principal_id,
                project_ref=project_id,
                session_id=raw_session_id,
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            return await self._worker.coordinator.final_flush(
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

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        """Swap the provider shared by direct reads and claim delivery.

        The caller holds module lifecycle ownership before invoking this,
        so a sidecar credential/runtime replacement cannot split these two
        consumers across provider instances.
        """

        self._provider = provider
        self._worker.replace_provider(provider)

    async def capture(self, request: CaptureRequest) -> CaptureReceipt:
        """Validate and persist one source capture without touching the provider."""

        if self._retired:
            return CaptureSkipped(reason="memory_operation_in_progress")
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
                if self._retired:
                    return CaptureSkipped(reason="memory_operation_in_progress")
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
        if result.outcome == "project_limit":
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
        effective_mode: Literal["keyword", "vector", "hybrid", "agentic"] | None = None,
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
        if not is_new_stored_memory_project_id(project_id):
            return OperationFailed(error="memory_access_denied")
        if len(query_bytes) > MAX_QUERY_BYTES:
            return OperationFailed(error="memory_input_too_large")

        if self._clear_active or self._is_maintenance_open():
            return OperationFailed(error="memory_clear_failed")

        agentic_started = (
            monotonic()
            if policy.mode == "agentic" and policy.timeout_seconds is not None
            else None
        )
        agentic_deadline = (
            agentic_started + float(policy.timeout_seconds)
            if agentic_started is not None
            else None
        )
        if agentic_deadline is None:
            return await self._recall_validated(
                normalized_query,
                policy=policy,
                principal_id=principal_id,
                project_id=project_id,
                current_session_id=current_session_id,
                effective_mode=effective_mode,
                agentic_deadline=None,
                agentic_telemetry=None,
            )
        agentic_telemetry = AgenticRecallTelemetry()
        try:
            remaining = _remaining_timeout(agentic_deadline)
            result = await asyncio.wait_for(
                self._recall_validated(
                    normalized_query,
                    policy=policy,
                    principal_id=principal_id,
                    project_id=project_id,
                    current_session_id=current_session_id,
                    effective_mode=effective_mode,
                    agentic_deadline=agentic_deadline,
                    agentic_telemetry=agentic_telemetry,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            result = OperationFailed(error="memory_provider_timeout")
        except BaseException:
            _log_agentic_recall_telemetry(
                started=agentic_started,
                telemetry=agentic_telemetry,
                result=None,
            )
            raise
        _log_agentic_recall_telemetry(
            started=agentic_started,
            telemetry=agentic_telemetry,
            result=result,
        )
        return result

    async def _recall_validated(
        self,
        normalized_query: str,
        *,
        policy: RecallPolicy,
        principal_id: str,
        project_id: str,
        current_session_id: str | None,
        effective_mode: Literal["keyword", "vector", "hybrid", "agentic"] | None,
        agentic_deadline: float | None,
        agentic_telemetry: AgenticRecallTelemetry | None,
    ) -> RecallResult:
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
            mode_was_pre_resolved = effective_mode is not None
            if effective_mode is None:
                effective_mode = await self.resolve_recall_mode(
                    policy,
                    timeout_seconds=(
                        _remaining_timeout(agentic_deadline)
                        if agentic_deadline is not None
                        else None
                    ),
                    agentic_telemetry=agentic_telemetry,
                )
                if isinstance(effective_mode, OperationFailed):
                    return effective_mode
            if effective_mode == "agentic":
                if requested_mode != "agentic" or agentic_deadline is None:
                    return OperationFailed(error="memory_capability_unavailable")
                if mode_was_pre_resolved:
                    resolved_mode = await self.resolve_recall_mode(
                        policy,
                        timeout_seconds=_remaining_timeout(agentic_deadline),
                        agentic_telemetry=agentic_telemetry,
                    )
                    if resolved_mode != "agentic":
                        return OperationFailed(error="memory_capability_unavailable")
                provider_timeout = _remaining_timeout(agentic_deadline)
            else:
                provider_timeout = None
            result = await self._provider_read(
                lambda: self._provider.search(
                    principal_id,
                    project_id,
                    normalized_query,
                    policy.max_results,
                    method=effective_mode,
                    include_profile=policy.include_profile,
                    session_ref=session_ref,
                    timeout_seconds=provider_timeout,
                    agentic_telemetry=agentic_telemetry,
                ),
                timeout_seconds=provider_timeout,
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

    async def resolve_recall_mode(
        self,
        policy: RecallPolicy,
        *,
        timeout_seconds: float | None = None,
        agentic_telemetry: AgenticRecallTelemetry | None = None,
    ) -> Literal["keyword", "vector", "hybrid", "agentic"] | OperationFailed:
        if policy.mode == "agentic":
            if not bool(getattr(self._provider, "agentic_budget_enforced", False)):
                return OperationFailed(error="memory_capability_unavailable")
        if policy.mode == "keyword":
            return "keyword"
        try:
            health = await asyncio.wait_for(
                self._provider.health_snapshot(),
                timeout=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else PROVIDER_READ_TIMEOUT_SECONDS
                ),
            )
            embed_available = health.capabilities.get("embed") is True
            agentic_available = (
                embed_available
                and health.capabilities.get("llm") is True
                and health.capabilities.get("rerank") is True
                and "agentic_search" not in health.disabled_features
            )
        except (asyncio.TimeoutError, MemoryProviderFailure) as failure:
            if agentic_telemetry is not None and (
                isinstance(failure, asyncio.TimeoutError)
                or failure.error == "memory_provider_timeout"
            ):
                agentic_telemetry.timed_out = True
            embed_available = False
            agentic_available = False
        except Exception:
            embed_available = False
            agentic_available = False
        if policy.mode == "agentic":
            if not agentic_available:
                return OperationFailed(error="memory_capability_unavailable")
            return "agentic"
        if policy.mode == "auto":
            return "hybrid" if embed_available else "keyword"
        if not embed_available:
            return OperationFailed(error="memory_capability_unavailable")
        return policy.mode

    async def profile(self, *, principal_id: str, project_id: str) -> MemoryResult:
        """Return a bounded provider profile result or one closed error category."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        if not is_principal_id(principal_id):
            return OperationFailed(error="memory_access_denied")
        if not is_new_stored_memory_project_id(project_id):
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

    async def list_episodes(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int = 1,
        page_size: int = MAX_LIST_PAGE_SIZE,
    ) -> MemoryListResult:
        """Return one bounded page of processed episodes or a closed error."""

        invalid = self._list_request_error(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
        )
        if invalid is not None:
            return invalid
        async with self._lifecycle_lock:
            return await self._list_episodes_under_lifecycle(
                principal_id=principal_id,
                project_id=project_id,
                page=page,
                page_size=page_size,
            )

    @asynccontextmanager
    async def concurrent_episode_lists(
        self,
    ) -> AsyncIterator[Callable[..., Awaitable[MemoryListResult]]]:
        """Fence one aggregate read while allowing its provider calls to overlap."""

        async with self._lifecycle_lock:
            yield self._list_episodes_under_lifecycle

    def _list_request_error(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> OperationFailed | None:
        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        if not is_principal_id(principal_id):
            return OperationFailed(error="memory_access_denied")
        if not is_new_stored_memory_project_id(project_id):
            return OperationFailed(error="memory_access_denied")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_LIST_PAGE_SIZE
        ):
            return OperationFailed(error="memory_invalid_input")
        if self._clear_active or self._is_maintenance_open():
            return OperationFailed(error="memory_clear_failed")
        return None

    async def _list_episodes_under_lifecycle(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListResult:
        invalid = self._list_request_error(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
        )
        if invalid is not None:
            return invalid
        try:
            meta = await self._store_call(self._store.ensure_meta)
        except Exception:
            return OperationFailed(error="memory_store_unavailable")
        if meta.clear_in_progress:
            return OperationFailed(error="memory_clear_failed")
        result = await self._provider_list_read(
            lambda: self._provider.list_episodes(
                principal_id,
                project_id,
                page,
                page_size,
            )
        )
        if isinstance(result, OperationFailed):
            return result
        return self._bounded_list_page(
            result,
            project_id=project_id,
            page=page,
            page_size=page_size,
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
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[MemoryItem, ...] | OperationFailed:
        try:
            return await asyncio.wait_for(
                operation(),
                timeout=timeout_seconds or PROVIDER_READ_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return OperationFailed(error="memory_provider_timeout")
        except MemoryProviderFailure as failure:
            return OperationFailed(error=_provider_error_code(failure, "memory_processing_failed"))
        except Exception:
            return OperationFailed(error="memory_processing_failed")

    async def _provider_list_read(
        self,
        operation: Callable[[], Awaitable[MemoryListPage]],
    ) -> MemoryListPage | OperationFailed:
        try:
            return await asyncio.wait_for(
                operation(),
                timeout=PROVIDER_READ_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return OperationFailed(error="memory_provider_timeout")
        except MemoryProviderFailure as failure:
            return OperationFailed(error=_provider_error_code(failure, "memory_processing_failed"))
        except Exception:
            return OperationFailed(error="memory_processing_failed")

    def _bounded_list_page(
        self,
        result: MemoryListPage,
        *,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListResult:
        if (
            not isinstance(result, MemoryListPage)
            or isinstance(result.page, bool)
            or not isinstance(result.page, int)
            or result.page != page
            or isinstance(result.page_size, bool)
            or not isinstance(result.page_size, int)
            or result.page_size != page_size
            or not isinstance(result.items, tuple)
            or isinstance(result.count, bool)
            or not isinstance(result.count, int)
            or result.count != len(result.items)
            or result.count > page_size
            or isinstance(result.total_count, bool)
            or not isinstance(result.total_count, int)
            or result.total_count < result.count
            or result.count != min(
                page_size,
                max(result.total_count - (page - 1) * page_size, 0),
            )
            or result.status != "ok"
            or not isinstance(result.warnings, tuple)
            or any(warning != "memory_list_truncated" for warning in result.warnings)
        ):
            return OperationFailed(error="memory_provider_response_invalid")
        total_bytes = 0
        seen_ids: set[str] = set()
        previous_instant: datetime | None = None
        for item in result.items:
            instant = _list_timestamp_instant(item.timestamp) if isinstance(
                item,
                MemoryListItem,
            ) else None
            if (
                not isinstance(item, MemoryListItem)
                or item.kind != "episode"
                or item.project != project_id
                or not _valid_list_identifier(item.id)
                or item.id in seen_ids
                or instant is None
                or (
                    previous_instant is not None
                    and instant > previous_instant
                )
            ):
                return OperationFailed(error="memory_provider_response_invalid")
            seen_ids.add(item.id)
            previous_instant = instant
            for value, allow_empty in (
                (item.subject, True),
                (item.summary, True),
                (item.body, False),
            ):
                encoded = _list_text_bytes(value, allow_empty=allow_empty)
                if encoded is None:
                    return OperationFailed(error="memory_provider_response_invalid")
                total_bytes += len(encoded)
            total_bytes += len(item.id.encode("utf-8"))
            total_bytes += len(item.timestamp.encode("utf-8"))
            total_bytes += len(item.project.encode("utf-8"))
            if total_bytes > MAX_PROVIDER_RESULT_BYTES:
                return OperationFailed(error="memory_provider_response_invalid")
        return result

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
            or not is_writable_memory_project_id(request.project_id)
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
        if self._retired:
            return False
        try:
            value = self._enabled_source() if callable(self._enabled_source) else self._enabled_source
        except Exception:
            return False
        return bool(value)

    def _is_maintenance_open(self) -> bool:
        try:
            return bool(self._maintenance_open())
        except Exception:
            return True

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

    async def _store_call(self, method: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await run_blocking(method, *args, **kwargs)

    def _default_free_disk_bytes(self) -> int:
        return int(shutil.disk_usage(self._store.path.parent).free)

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


def _list_text_bytes(value: object, *, allow_empty: bool) -> bytes | None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or (not allow_empty and not value)
        or "\x00" in value
    ):
        return None
    encoded = _utf8_bytes(value)
    if encoded is None or len(encoded) > MAX_PROVIDER_ITEM_BYTES:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t", "\r"} for character in value):
        return None
    return encoded


def _valid_list_identifier(value: object) -> bool:
    encoded = _utf8_bytes(value) if isinstance(value, str) else None
    return bool(value) and encoded is not None and len(encoded) <= 128 and "\x00" not in value


def _list_timestamp_instant(value: object) -> datetime | None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _RFC3339_TIMESTAMP_RE.fullmatch(value) is None
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


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


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0.001:
        raise asyncio.TimeoutError
    return remaining


def _log_agentic_recall_telemetry(
    *,
    started: float,
    telemetry: AgenticRecallTelemetry,
    result: RecallResult | None,
) -> None:
    success = isinstance(result, RecallItems)
    timeout = telemetry.timed_out or (
        isinstance(result, OperationFailed) and result.error == "memory_provider_timeout"
    )
    logger.info(
        "Memory recall telemetry mode=agentic round=%s duration_ms=%s success=%s timeout=%s",
        telemetry.round,
        max(0, int((monotonic() - started) * 1000)),
        str(success).lower(),
        str(timeout).lower(),
    )
