"""The provider-independent MemoryModule interface."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import unicodedata
import weakref
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal, TypeVar

from config import paths
from core.blocking import run_blocking
from avibe_memory.attachments import (
    AttachmentCleanupUnprovenError,
    AttachmentPinError,
    AttachmentPinStore,
    PinnedBundle,
)
from avibe_memory.provider_root import ProviderRoot
from avibe_memory.everos import (
    AgenticRecallTelemetry,
    MemoryProviderFailure,
    MemoryProviderPort,
)
from vibe.memory_project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
    is_writable_memory_project_id,
)
from avibe_memory.store import (
    MemoryStore,
    VolatileAdmission,
    derive_assistant_memory_owner_id,
    is_memory_owner_id,
    is_principal_id,
)
from avibe_memory.types import (
    CaptureAccepted,
    CaptureAttachment,
    CaptureDuplicate,
    CaptureReceipt,
    CaptureRequest,
    CaptureSkipped,
    MemoryErrorCode,
    MemoryItem,
    MemoryItems,
    MemoryListItem,
    MemoryListPage,
    MemoryListResult,
    MemoryOrigin,
    MemoryProfile,
    MemoryProfileExplicitInfo,
    MemoryProfileTrait,
    MemoryResult,
    MAX_MEMORY_LIST_PAGE_SIZE,
    MAX_MEMORY_SEARCH_RESULTS,
    OperationFailed,
    ProviderSearchItem,
    RecallItems,
    RecallPolicy,
    RecallResult,
    is_memory_error_code,
)
from avibe_memory.writer import (
    BestEffortMemoryWriter,
    CaptureOfferOutcome,
    WriterReservation,
)

if TYPE_CHECKING:
    from core.inbound_attachment_lease import InboundAttachmentLease


MAX_CAPTURE_IDENTIFIER_BYTES = 1024
MAX_CAPTURE_ATTACHMENTS = 8
MIN_FREE_DISK_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_TIMESTAMP_MS = 4_102_444_800_000
DEFAULT_SEARCH_LIMIT = 8
DEFAULT_LIST_PAGE_SIZE = 20
PROVIDER_READ_TIMEOUT_SECONDS = 20.0


class _HeldCaptureAdmission:
    """One active claim on an exact-session capture fence."""

    def __init__(
        self,
        module: "MemoryModule",
        lock: asyncio.Lock,
        key: tuple[str, str, str],
    ) -> None:
        self.module = module
        self.lock = lock
        self.key = key
        self.active = True


class _CaptureReservation:
    """One O(1) FIFO registration for exact-session capture work."""

    def __init__(
        self,
        module: "MemoryModule",
        key: tuple[str, str, str],
        predecessor: asyncio.Future[None] | None,
    ) -> None:
        self.module = module
        self.key = key
        self.predecessor = predecessor
        self.completion = asyncio.get_running_loop().create_future()
        self.active = True

    async def wait_for_turn(self) -> None:
        if self.predecessor is not None:
            await asyncio.shield(self.predecessor)

    def complete(self) -> None:
        if not self.active:
            return
        self.active = False
        if self.predecessor is None or self.predecessor.done():
            self._resolve_completion()
            return
        self.predecessor.add_done_callback(
            lambda _predecessor: self._resolve_completion()
        )

    def _resolve_completion(self) -> None:
        if not self.completion.done():
            self.completion.set_result(None)


logger = logging.getLogger(__name__)
_ProviderReadResult = TypeVar("_ProviderReadResult")


_ROOT_LIFECYCLE_LOCKS: dict[str, asyncio.Lock] = {}
_RFC3339_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)


class MemorySessionLifecycleBusyError(RuntimeError):
    """Raised when a destructive session transition cannot acquire its fence."""

    code = "memory_session_lifecycle_busy"


class MemoryModule:
    """Own local capture and direct reads without exposing storage internals."""

    def __init__(
        self,
        store: MemoryStore,
        provider: MemoryProviderPort,
        *,
        enabled: bool | Callable[[], bool] = False,
        disk_free_bytes: Callable[[], int] | None = None,
        provider_root: Path | None = None,
        provider_root_owner: ProviderRoot | None = None,
        runtime_active: Callable[[], bool] | None = None,
        processing_event: Callable[..., Awaitable[bool]] | None = None,
        ambiguous_stop_reap: Callable[[], Awaitable[bool] | bool] | None = None,
        writer: BestEffortMemoryWriter | None = None,
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
        self._runtime_active = runtime_active or (lambda: True)
        self.provider_root = provider_root_owner or ProviderRoot(
            self._provider_root,
            effective_home=self._effective_home,
        )
        self._lifecycle_lock = asyncio.Lock()
        self._capture_admission_locks: weakref.WeakValueDictionary[
            tuple[str, str, str], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._capture_reservation_tails: dict[
            tuple[str, str, str], _CaptureReservation
        ] = {}
        self._invalid_capture_admission_lock = asyncio.Lock()
        attachments_available = False
        self._attachment_store: AttachmentPinStore | None = None
        try:
            self._attachment_store = attachment_store or AttachmentPinStore(
                effective_home=self._effective_home
            )
        except AttachmentPinError:
            logger.warning(
                "Memory attachment storage is unavailable; text capture remains enabled"
            )
        else:
            try:
                self._attachment_store.clear_all()
                attachments_available = True
            except AttachmentPinError:
                logger.warning(
                    "Memory attachment startup cleanup failed; text capture remains enabled"
                )
        self._writer = writer or BestEffortMemoryWriter(
            store=store,
            provider=provider,
            enabled=self._is_enabled,
            processing_event=processing_event,
            attachment_store=self._attachment_store,
            ambiguous_stop_reap=ambiguous_stop_reap,
        )
        if not attachments_available:
            self._writer.disable_attachment_intake()

    @property
    def attachment_intake_enabled(self) -> bool:
        """Whether new attachment captures can enter the volatile writer."""

        return self._writer.attachments_enabled

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
        """Synchronously fence new volatile writer admissions."""

        self._writer.pause_intake()

    async def quiesce_claims(self, *, timeout_seconds: float | None = None) -> bool:
        """Fence claims and join in-flight add and flush work under one deadline."""

        return await self._writer.quiesce(
            timeout_seconds=30.0 if timeout_seconds is None else timeout_seconds
        )

    async def quiesce_claims_for_destructive_reset(self) -> bool:
        """Fence claims using the module's configured destructive-work budget."""

        return await self._writer.quiesce(timeout_seconds=5.0)

    def resume_claims(self) -> None:
        """Permit add and flush claims after lifecycle recovery succeeds."""

        self._writer.resume_intake()

    async def close_writer(self) -> None:
        """Drop volatile work during shutdown or runtime replacement."""

        await self._writer.close()

    def reserve_capture_capacity(
        self,
    ) -> WriterReservation | Literal["full", "disabled", "unavailable"]:
        """Claim one volatile capture slot before deferred work starts."""

        return self._writer.reserve_pending()

    def release_capture_capacity(self, reservation: object) -> None:
        """Release a pending slot that was not handed to the writer queue."""

        if isinstance(reservation, WriterReservation):
            if reservation.active and not reservation.handed_off:
                reservation.abandon()

    async def wait_writer_idle_for_tests(self, *, timeout_seconds: float = 5.0) -> None:
        await self._writer.wait_idle_for_tests(timeout_seconds=timeout_seconds)

    def offer_barrier(self, raw_session_id: str) -> str:
        """Offer a provider barrier without waiting for capture delivery."""

        return self._writer.offer_barrier(raw_session_id)

    def replace_provider(self, provider: MemoryProviderPort) -> None:
        """Swap the provider shared by direct reads and claim delivery.

        The caller holds module lifecycle ownership before invoking this,
        so a sidecar credential/runtime replacement cannot split these two
        consumers across provider instances.
        """

        self._provider = provider
        self._writer.replace_provider(provider)

    async def capture(
        self,
        request: CaptureRequest,
        *,
        source_lease: InboundAttachmentLease | None = None,
        admission: object = None,
        capacity_reservation: object = None,
    ) -> CaptureReceipt:
        """Validate and persist one source capture without touching the provider."""

        if not self._owns_runtime():
            return CaptureSkipped(reason="memory_operation_in_progress")
        if not self._is_enabled():
            return CaptureSkipped(reason="memory_disabled")

        reservation = capacity_reservation
        if reservation is None:
            reservation = self.reserve_capture_capacity()
        if isinstance(reservation, str):
            if reservation == "full":
                return await self._skipped_with_missed("memory_queue_full")
            if reservation == "unavailable":
                return await self._skipped_with_missed("memory_sidecar_unavailable")
            return CaptureSkipped(reason="memory_operation_in_progress")
        if not isinstance(reservation, WriterReservation):
            return await self._skipped_with_missed("memory_invalid_input")

        try:
            admission_lock = self._capture_lock_for_request(request)
            if admission is None:
                key = self._capture_admission_key(
                    principal_id=getattr(request, "principal_id", None),
                    project_id=getattr(request, "project_id", None),
                    session_id=getattr(request, "session_id", None),
                )
                if key is not None:
                    capture_admission = self.reserve_capture_admission(
                        principal_id=key[0],
                        project_id=key[1],
                        session_id=key[2],
                    )
                    async with self.capture_admission(
                        principal_id=key[0],
                        project_id=key[1],
                        session_id=key[2],
                        reservation=capture_admission,
                    ):
                        return await self._capture_with_admission(
                            request,
                            source_lease=source_lease,
                            capacity_reservation=reservation,
                        )
                async with admission_lock:
                    return await self._capture_with_admission(
                        request,
                        source_lease=source_lease,
                        capacity_reservation=reservation,
                    )
            if not self._owns_capture_admission(admission, request, admission_lock):
                return await self._skipped_with_missed("memory_invalid_input")
            return await self._capture_with_admission(
                request,
                source_lease=source_lease,
                capacity_reservation=reservation,
            )
        finally:
            self.release_capture_capacity(reservation)

    @asynccontextmanager
    async def capture_admission(
        self,
        *,
        principal_id: str,
        project_id: str,
        session_id: str,
        reservation: object = None,
    ) -> AsyncIterator[_HeldCaptureAdmission]:
        """Acquire the exact-session fence before deferred capture work starts."""

        key = (principal_id, project_id, session_id)
        lock = self._capture_admission_lock(
            principal_id=principal_id,
            project_id=project_id,
            session_id=session_id,
        )
        ticket = reservation if isinstance(reservation, _CaptureReservation) else None
        if reservation is not None and not self._owns_capture_reservation(ticket, key):
            self.cancel_capture_reservation(reservation)
            raise ValueError("invalid Memory capture reservation")
        acquired = False
        try:
            if ticket is not None:
                await ticket.wait_for_turn()
            await lock.acquire()
            acquired = True
            admission = _HeldCaptureAdmission(self, lock, key)
            yield admission
        finally:
            if acquired:
                admission.active = False
                lock.release()
            if ticket is not None:
                self.cancel_capture_reservation(ticket)

    def reserve_capture_admission(
        self,
        *,
        principal_id: str,
        project_id: str,
        session_id: str,
    ) -> object:
        """Register exact-session capture order without waiting for earlier work."""

        key = self._capture_admission_key(
            principal_id=principal_id,
            project_id=project_id,
            session_id=session_id,
        )
        if key is None:
            raise ValueError("invalid Memory capture reservation scope")
        tail = self._capture_reservation_tails.get(key)
        predecessor = tail.completion if tail is not None else None
        reservation = _CaptureReservation(self, key, predecessor)
        self._capture_reservation_tails[key] = reservation
        reservation.completion.add_done_callback(
            lambda _completion: self._retire_capture_reservation(key, reservation)
        )
        return reservation

    def cancel_capture_reservation(self, reservation: object) -> None:
        """Complete an owned reservation on cancellation or scheduling failure."""

        if isinstance(reservation, _CaptureReservation) and reservation.module is self:
            reservation.complete()

    async def _wait_for_capture_reservation(
        self,
        reservation: object,
        deadline: float,
    ) -> None:
        if not isinstance(reservation, _CaptureReservation):
            raise ValueError("invalid Memory capture reservation")
        if reservation.predecessor is None:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(
            asyncio.shield(reservation.predecessor),
            timeout=remaining,
        )

    def _retire_capture_reservation(
        self,
        key: tuple[str, str, str],
        reservation: _CaptureReservation,
    ) -> None:
        if self._capture_reservation_tails.get(key) is reservation:
            self._capture_reservation_tails.pop(key, None)

    def _owns_capture_reservation(
        self,
        reservation: _CaptureReservation | None,
        key: tuple[str, str, str],
    ) -> bool:
        return bool(
            reservation is not None
            and reservation.active
            and reservation.module is self
            and reservation.key == key
        )

    async def _capture_with_admission(
        self,
        request: CaptureRequest,
        *,
        source_lease: InboundAttachmentLease | None,
        capacity_reservation: WriterReservation,
    ) -> CaptureReceipt:
        async with self._root_lifecycle_lock():
            if not self._owns_runtime():
                return CaptureSkipped(reason="memory_operation_in_progress")
            if not self._is_enabled():
                return CaptureSkipped(reason="memory_disabled")
            if not isinstance(request, CaptureRequest):
                return await self._skipped_with_missed("memory_invalid_input")

            normalized_text = self._normalize_text(request.text)
            validation_error = self._capture_validation_error(request, normalized_text)
            if validation_error is not None:
                return await self._skipped_with_missed(validation_error)
            if (
                request.attachments
                and not self._writer.attachments_enabled
                and not normalized_text.strip()
            ):
                return await self._skipped_with_missed("memory_store_unavailable")

            try:
                disk_free = int(await asyncio.to_thread(self._disk_free_bytes))
            except Exception:
                return await self._skipped_with_missed("memory_low_disk_space")
            if disk_free < MIN_FREE_DISK_BYTES:
                return await self._skipped_with_missed("memory_low_disk_space")
            return await self._capture_under_root(
                request,
                normalized_text,
                source_lease=source_lease,
                capacity_reservation=capacity_reservation,
            )

    def _owns_runtime(self) -> bool:
        try:
            return bool(self._runtime_active())
        except Exception:
            return False

    def _capture_lock_for_request(self, request: object) -> asyncio.Lock:
        if not isinstance(request, CaptureRequest):
            return self._invalid_capture_admission_lock
        return self._capture_admission_lock(
            principal_id=request.principal_id,
            project_id=request.project_id,
            session_id=request.session_id,
        )

    def _owns_capture_admission(
        self,
        admission: object,
        request: object,
        lock: asyncio.Lock,
    ) -> bool:
        return bool(
            isinstance(admission, _HeldCaptureAdmission)
            and admission.active
            and admission.module is self
            and admission.lock is lock
            and isinstance(request, CaptureRequest)
            and admission.key
            == (request.principal_id, request.project_id, request.session_id)
        )

    async def _capture_under_root(
        self,
        request: CaptureRequest,
        normalized_text: str,
        *,
        source_lease: InboundAttachmentLease | None,
        capacity_reservation: WriterReservation,
    ) -> CaptureReceipt:
        """Reserve, pin, and offer one capture to the volatile writer."""

        try:
            digest = await self._store_call(
                self._store.source_message_digest,
                request.source_message_id,
            )
        except Exception:
            return OperationFailed(error="memory_store_unavailable")
        binding = capacity_reservation.bind_digest(digest)
        if binding == "duplicate":
            return CaptureDuplicate()
        if binding == "unavailable":
            return await self._skipped_with_missed("memory_sidecar_unavailable")
        if binding == "disabled":
            return CaptureSkipped(reason="memory_operation_in_progress")
        reservation = capacity_reservation

        pinned_bundle: PinnedBundle | None = None
        try:
            if request.attachments and self._writer.attachments_enabled:
                if source_lease is None:
                    pinned_bundle = await run_blocking(
                        self._attachment_store.pin,
                        request.attachments,
                        on_cancel_result=self._release_cancelled_pinned_bundle,
                        on_cancel_error=self._handle_cancelled_attachment_failure,
                    )
                else:
                    pinned_bundle = await run_blocking(
                        self._attachment_store.pin,
                        request.attachments,
                        source_lease=source_lease,
                        on_cancel_result=self._release_cancelled_pinned_bundle,
                        on_cancel_error=self._handle_cancelled_attachment_failure,
                    )
            admission = await self._store_call(
                self._store.admit_volatile_capture,
                source_message_id=request.source_message_id,
                session_id=request.session_id,
                principal_id=request.principal_id,
                project_ref=request.project_id,
                provenance=request.provenance,
                occurred_at_ms=request.occurred_at_ms,
                max_provider_timestamp_ms=MAX_PROVIDER_TIMESTAMP_MS,
            )
        except asyncio.CancelledError:
            await self._release_unadmitted_capture(reservation, pinned_bundle)
            raise
        except Exception as error:
            if isinstance(error, AttachmentCleanupUnprovenError):
                self._writer.disable_attachment_intake()
            if isinstance(error, AttachmentPinError) and normalized_text.strip() and request.attachments:
                try:
                    admission = await self._store_call(
                        self._store.admit_volatile_capture,
                        source_message_id=request.source_message_id,
                        session_id=request.session_id,
                        principal_id=request.principal_id,
                        project_ref=request.project_id,
                        provenance=request.provenance,
                        occurred_at_ms=request.occurred_at_ms,
                        max_provider_timestamp_ms=MAX_PROVIDER_TIMESTAMP_MS,
                    )
                except asyncio.CancelledError:
                    await self._release_unadmitted_capture(reservation, pinned_bundle)
                    raise
                except Exception:
                    pass
                else:
                    return await self._complete_capture_admission(
                        reservation,
                        admission,
                        text=normalized_text,
                        bundle=None,
                        sender_name=request.sender_name,
                    )
            await self._release_unadmitted_capture(reservation, pinned_bundle)
            if isinstance(error, AttachmentPinError) and normalized_text.strip() and request.attachments:
                return CaptureSkipped(reason="memory_store_unavailable")
            if isinstance(error, AttachmentPinError):
                return await self._capture_pin_failure(error.error)
            if isinstance(error, UnicodeError):
                return await self._skipped_with_missed("memory_invalid_input")
            return OperationFailed(error="memory_store_unavailable")

        return await self._complete_capture_admission(
            reservation,
            admission,
            text=normalized_text,
            bundle=pinned_bundle,
            sender_name=request.sender_name,
        )

    async def _complete_capture_admission(
        self,
        reservation: WriterReservation,
        admission: VolatileAdmission,
        *,
        text: str,
        bundle: PinnedBundle | None,
        sender_name: str | None = None,
    ) -> CaptureReceipt:
        if admission.outcome == "accepted":
            return await self._offer_admitted_capture(
                reservation,
                admission,
                text=text,
                bundle=bundle,
                sender_name=sender_name,
            )

        await self._release_unadmitted_capture(reservation, bundle)
        if admission.outcome in {"project_limit", "timestamp_invalid"}:
            return CaptureSkipped(reason="memory_invalid_input")
        if admission.outcome == "clear_in_progress":
            return CaptureSkipped(reason="memory_operation_in_progress")
        return OperationFailed(error="memory_store_unavailable")

    async def _offer_admitted_capture(
        self,
        reservation: WriterReservation,
        admission: VolatileAdmission,
        *,
        text: str,
        bundle: PinnedBundle | None,
        sender_name: str | None = None,
    ) -> CaptureReceipt:
        outcome: CaptureOfferOutcome = self._writer.offer_capture(
            reservation,
            admission,
            text=text,
            attachments=(),
            bundle=bundle,
            sender_name=sender_name,
        )
        if outcome == "queued":
            return CaptureAccepted(
                captured_attachment_count=(
                    len(bundle.attachments) if bundle is not None else 0
                )
            )
        await self._release_unadmitted_capture(reservation, bundle)
        if outcome == "full":
            return await self._skipped_with_missed("memory_queue_full")
        if outcome == "unavailable":
            return await self._skipped_with_missed("memory_sidecar_unavailable")
        return await self._skipped_with_missed("memory_operation_in_progress")

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
        if self._attachment_store is None:
            self._writer.disable_attachment_intake()
            return
        try:
            await run_blocking(
                self._attachment_store.release,
                bundle_id,
                on_cancel_error=lambda _error: self._writer.disable_attachment_intake(),
            )
        except Exception:
            self._writer.disable_attachment_intake()

    async def _release_unadmitted_capture(
        self,
        reservation: WriterReservation,
        bundle: PinnedBundle | None,
    ) -> None:
        try:
            if bundle is not None:
                await self._release_unadmitted_bundle(bundle.bundle_id)
        finally:
            reservation.abandon()

    def _release_cancelled_pinned_bundle(self, bundle: PinnedBundle) -> None:
        if self._attachment_store is None:
            self._writer.disable_attachment_intake()
            return
        try:
            self._attachment_store.release(bundle.bundle_id)
        except Exception:
            self._writer.disable_attachment_intake()

    def _handle_cancelled_attachment_failure(self, error: BaseException) -> None:
        if isinstance(error, AttachmentCleanupUnprovenError):
            self._writer.disable_attachment_intake()

    async def search(
        self,
        query: str,
        *,
        principal_id: str,
        project_id: str,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> MemoryResult:
        """Compatibility wrapper for the default hybrid recall policy."""

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
        """Execute one capability-gated, dual-owner recall decision."""

        if not self._is_enabled():
            return OperationFailed(error="memory_disabled")
        if not isinstance(policy, RecallPolicy):
            return OperationFailed(error="memory_invalid_input")
        normalized_query = self._normalize_text(query)
        if _utf8_bytes(normalized_query) is None or not normalized_query.strip():
            return OperationFailed(error="memory_invalid_input")
        if not is_principal_id(principal_id):
            return OperationFailed(error="memory_access_denied")
        if not is_new_stored_memory_project_id(project_id):
            return OperationFailed(error="memory_access_denied")
        if not self._owns_runtime():
            return OperationFailed(error="memory_operation_in_progress")

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
                return OperationFailed(error="memory_operation_in_progress")
            owner_ids = (
                principal_id,
                derive_assistant_memory_owner_id(principal_id),
            )
            session_refs = [None, None]
            if policy.include_current_session:
                if not isinstance(current_session_id, str) or not current_session_id.strip():
                    return OperationFailed(error="memory_invalid_input")
                try:
                    session_refs = list(
                        await asyncio.gather(
                            *(
                                self._store_call(
                                    self._store.provider_session_ref,
                                    principal_id=principal_id,
                                    project_ref=project_id,
                                    session_id=current_session_id.strip(),
                                    memory_owner_id=owner_id,
                                )
                                for owner_id in owner_ids
                            )
                        )
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
            leg_methods = (
                effective_mode,
                "hybrid" if effective_mode == "agentic" else effective_mode,
            )
            results = await asyncio.gather(
                *(
                    self._provider_read(
                        lambda owner_id=owner_id, method=method, session_ref=session_ref, index=index: self._provider.search(
                            owner_id,
                            project_id,
                            normalized_query,
                            policy.max_results,
                            method=method,
                            include_profile=policy.include_profile,
                            session_ref=session_ref,
                            timeout_seconds=provider_timeout if index == 0 else None,
                            agentic_telemetry=agentic_telemetry if index == 0 else None,
                        ),
                        timeout_seconds=provider_timeout,
                    )
                    for index, (owner_id, method, session_ref) in enumerate(
                        zip(owner_ids, leg_methods, session_refs)
                    )
                )
            )
        successful: list[tuple[ProviderSearchItem, ...]] = []
        leg_succeeded: list[bool] = []
        first_failure: OperationFailed | None = None
        for owner_id, result in zip(owner_ids, results):
            if isinstance(result, OperationFailed):
                if first_failure is None:
                    first_failure = result
                successful.append(())
                leg_succeeded.append(False)
                continue
            bounded = self._bounded_provider_search_items(
                result,
                owner_id=owner_id,
                limit=policy.max_results,
            )
            if isinstance(bounded, OperationFailed):
                if first_failure is None:
                    first_failure = bounded
                successful.append(())
                leg_succeeded.append(False)
                continue
            successful.append(bounded)
            leg_succeeded.append(True)
        succeeded_count = sum(leg_succeeded)
        if succeeded_count == 0:
            return first_failure or OperationFailed(error="memory_processing_failed")
        merged = _merge_owner_search_items(
            user_items=successful[0],
            assistant_items=successful[1],
            same_method=leg_methods[0] == leg_methods[1],
            limit=policy.max_results,
        )
        return RecallItems(
            items=merged,
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            current_session_overlay=any(ref is not None for ref in session_refs),
            warnings=("memory_search_partial",) if succeeded_count == 1 else (),
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
        if not self._owns_runtime():
            return OperationFailed(error="memory_operation_in_progress")

        async with self._lifecycle_lock:
            if not self._is_enabled():
                return OperationFailed(error="memory_disabled")
            try:
                meta = await self._store_call(self._store.ensure_meta)
            except Exception:
                return OperationFailed(error="memory_store_unavailable")
            if meta.clear_in_progress:
                return OperationFailed(error="memory_operation_in_progress")
            owner_ids = (
                principal_id,
                derive_assistant_memory_owner_id(principal_id),
            )
            results = await asyncio.gather(
                *(
                    self._provider_read(lambda owner_id=owner_id: self._provider.profile(owner_id, project_id))
                    for owner_id in owner_ids
                )
            )
        items: list[MemoryItem] = []
        first_failure: OperationFailed | None = None
        succeeded = 0
        for origin, result in zip(("user", "agent"), results):
            if isinstance(result, OperationFailed):
                if first_failure is None:
                    first_failure = result
                continue
            bounded = self._bounded_items(result, limit=MAX_MEMORY_SEARCH_RESULTS)
            if isinstance(bounded, OperationFailed):
                if first_failure is None:
                    first_failure = bounded
                continue
            succeeded += 1
            items.extend(replace(item, origin=origin) for item in bounded.items)
        if succeeded == 0:
            return first_failure or OperationFailed(error="memory_processing_failed")
        return MemoryItems(
            items=tuple(items),
            warnings=("memory_search_partial",) if succeeded == 1 else (),
        )

    async def list_episodes(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int = 1,
        page_size: int = DEFAULT_LIST_PAGE_SIZE,
        origin: MemoryOrigin = "user",
    ) -> MemoryListResult:
        """Return one bounded page of processed episodes or a closed error."""

        invalid = self._list_request_error(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
            origin=origin,
        )
        if invalid is not None:
            return invalid
        async with self._lifecycle_lock:
            return await self._list_episodes_under_lifecycle(
                principal_id=principal_id,
                project_id=project_id,
                page=page,
                page_size=page_size,
                origin=origin,
            )

    @asynccontextmanager
    async def concurrent_episode_lists(
        self,
        *,
        deadline: float,
    ) -> AsyncIterator[Callable[..., Awaitable[MemoryListResult]]]:
        """Fence one aggregate read while allowing its provider calls to overlap."""

        def unavailable(
            error: MemoryErrorCode,
        ) -> Callable[..., Awaitable[MemoryListResult]]:
            async def result(**_kwargs: Any) -> MemoryListResult:
                return OperationFailed(error=error)

            return result

        if not self._owns_runtime():
            yield unavailable("memory_operation_in_progress")
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            yield unavailable("memory_provider_timeout")
            return
        try:
            await asyncio.wait_for(
                self._lifecycle_lock.acquire(),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            yield unavailable("memory_provider_timeout")
            return
        try:
            if not self._owns_runtime():
                yield unavailable("memory_operation_in_progress")
                return
            try:
                meta = await self._store_call(self._store.ensure_meta)
            except Exception:
                yield unavailable("memory_store_unavailable")
                return
            if meta.clear_in_progress:
                yield unavailable("memory_operation_in_progress")
                return
            if monotonic() >= deadline:
                yield unavailable("memory_provider_timeout")
                return
            yield self._list_episodes_after_store_check
        finally:
            self._lifecycle_lock.release()

    def _list_request_error(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
        origin: MemoryOrigin,
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
            or not 1 <= page_size <= MAX_MEMORY_LIST_PAGE_SIZE
            or origin not in ("user", "agent")
        ):
            return OperationFailed(error="memory_invalid_input")
        if not self._owns_runtime():
            return OperationFailed(error="memory_operation_in_progress")
        return None

    async def _list_episodes_under_lifecycle(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
        origin: MemoryOrigin,
    ) -> MemoryListResult:
        invalid = self._list_request_error(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
            origin=origin,
        )
        if invalid is not None:
            return invalid
        try:
            meta = await self._store_call(self._store.ensure_meta)
        except Exception:
            return OperationFailed(error="memory_store_unavailable")
        if meta.clear_in_progress:
            return OperationFailed(error="memory_operation_in_progress")
        return await self._list_episodes_after_store_check(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
            origin=origin,
        )

    async def _list_episodes_after_store_check(
        self,
        *,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
        origin: MemoryOrigin,
    ) -> MemoryListResult:
        invalid = self._list_request_error(
            principal_id=principal_id,
            project_id=project_id,
            page=page,
            page_size=page_size,
            origin=origin,
        )
        if invalid is not None:
            return invalid
        owner_id = (
            principal_id
            if origin == "user"
            else derive_assistant_memory_owner_id(principal_id)
        )
        result = await self._provider_list_read(
            lambda: self._provider.list_episodes(
                owner_id,
                project_id,
                page,
                page_size,
            )
        )
        if isinstance(result, OperationFailed):
            return result
        bounded = self._bounded_list_page(
            result,
            project_id=project_id,
            page=page,
            page_size=page_size,
        )
        if isinstance(bounded, OperationFailed):
            return bounded
        return replace(
            bounded,
            items=tuple(replace(item, origin=origin) for item in bounded.items),
        )

    async def _skipped_with_missed(self, error: MemoryErrorCode) -> CaptureReceipt:
        try:
            status_error = error if error == "memory_low_disk_space" else None
            await self._store_call(self._store.record_capture_skip, status_error)
        except Exception:
            return OperationFailed(error="memory_store_unavailable")
        return CaptureSkipped(reason=error)

    async def _provider_read(
        self,
        operation: Callable[[], Awaitable[tuple[_ProviderReadResult, ...]]],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[_ProviderReadResult, ...] | OperationFailed:
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

    def _bounded_provider_search_items(
        self,
        items: tuple[ProviderSearchItem, ...],
        *,
        owner_id: str,
        limit: int,
    ) -> tuple[ProviderSearchItem, ...] | OperationFailed:
        if not isinstance(items, tuple) or len(items) > limit or not is_memory_owner_id(owner_id):
            return OperationFailed(error="memory_provider_response_invalid")
        bounded = self._bounded_items(tuple(item.item for item in items), limit=limit) if all(
            isinstance(item, ProviderSearchItem) for item in items
        ) else OperationFailed(error="memory_provider_response_invalid")
        if isinstance(bounded, OperationFailed):
            return bounded
        for item in items:
            if (
                item.queried_owner != owner_id
                or item.item.origin is not None
                or isinstance(item.provider_rank, bool)
                or not isinstance(item.provider_rank, int)
                or item.provider_rank < 0
                or (
                    item.score is not None
                    and (
                        isinstance(item.score, bool)
                        or not isinstance(item.score, (int, float))
                        or not math.isfinite(item.score)
                    )
                )
                or (
                    item.episode_id is not None
                    and (
                        not isinstance(item.episode_id, str)
                        or not item.episode_id
                        or (episode_id_bytes := _utf8_bytes(item.episode_id)) is None
                        or len(episode_id_bytes) > 256
                    )
                )
                or (item.timestamp is not None and _list_timestamp_instant(item.timestamp) is None)
            ):
                return OperationFailed(error="memory_provider_response_invalid")
        return items

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
                or item.origin is not None
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
                if _list_text_bytes(value, allow_empty=allow_empty) is None:
                    return OperationFailed(error="memory_provider_response_invalid")
        return result

    def _bounded_items(self, items: tuple[MemoryItem, ...], *, limit: int) -> MemoryResult:
        if not isinstance(items, tuple) or len(items) > limit:
            return OperationFailed(error="memory_provider_response_invalid")
        for item in items:
            if not isinstance(item, MemoryItem) or item.kind not in {"profile", "episode", "fact"}:
                return OperationFailed(error="memory_provider_response_invalid")
            item_text = _utf8_bytes(item.text) if isinstance(item.text, str) else None
            if item_text is None or not item.text or "\x00" in item.text:
                return OperationFailed(error="memory_provider_response_invalid")
            if item.date is not None:
                date_bytes = _utf8_bytes(item.date) if isinstance(item.date, str) else None
                if date_bytes is None or len(date_bytes) > 64:
                    return OperationFailed(error="memory_provider_response_invalid")
                try:
                    date.fromisoformat(item.date)
                except ValueError:
                    return OperationFailed(error="memory_provider_response_invalid")
            if item.profile is not None:
                if item.kind != "profile":
                    return OperationFailed(error="memory_provider_response_invalid")
                # EverOS owns profile payload sizing; Avibe still validates the
                # structured projection before returning it.
                if _profile_bytes(item.profile) is None:
                    return OperationFailed(error="memory_provider_response_invalid")
            if item.origin not in {None, "user", "agent", "both"}:
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
        if _utf8_bytes(normalized_text) is None:
            return "memory_invalid_input"
        if not normalized_text.strip() and not request.attachments:
            return "memory_invalid_input"
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
        if not self._owns_runtime():
            return False
        try:
            value = self._enabled_source() if callable(self._enabled_source) else self._enabled_source
        except Exception:
            return False
        return bool(value)

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

        key = self._capture_admission_key(
            principal_id=principal_id,
            project_id=project_id,
            session_id=session_id,
        )
        if key is None:
            return self._invalid_capture_admission_lock
        lock = self._capture_admission_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._capture_admission_locks[key] = lock
        return lock

    @staticmethod
    def _capture_admission_key(
        *,
        principal_id: object,
        project_id: object,
        session_id: object,
    ) -> tuple[str, str, str] | None:
        if not all(
            isinstance(value, str)
            for value in (principal_id, project_id, session_id)
        ):
            return None
        return principal_id, project_id, session_id

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


def _merge_owner_search_items(
    *,
    user_items: tuple[ProviderSearchItem, ...],
    assistant_items: tuple[ProviderSearchItem, ...],
    same_method: bool,
    limit: int,
) -> tuple[MemoryItem, ...]:
    if same_method:
        ranked: list[tuple[ProviderSearchItem, Literal["user", "agent"]]] = [
            *((item, "user") for item in user_items),
            *((item, "agent") for item in assistant_items),
        ]
        ranked.sort(key=lambda value: _same_method_rank_key(value[0]))
    else:
        user_ranked = sorted(user_items, key=_per_leg_rank_key)
        assistant_ranked = sorted(assistant_items, key=_per_leg_rank_key)
        ranked = []
        for index in range(max(len(user_ranked), len(assistant_ranked))):
            if index < len(user_ranked):
                ranked.append((user_ranked[index], "user"))
            if index < len(assistant_ranked):
                ranked.append((assistant_ranked[index], "agent"))

    merged: list[MemoryItem] = []
    seen: dict[str, int] = {}
    for provider_item, origin in ranked:
        item = replace(provider_item.item, origin=origin)
        normalized_text = MemoryModule._normalize_text(item.text)
        existing_index = seen.get(normalized_text)
        if existing_index is not None:
            existing = merged[existing_index]
            if existing.origin != origin:
                merged[existing_index] = replace(existing, origin="both")
            continue
        if len(merged) >= limit:
            continue
        seen[normalized_text] = len(merged)
        merged.append(item)
    return tuple(merged)


def _same_method_rank_key(item: ProviderSearchItem) -> tuple[object, ...]:
    timestamp = _list_timestamp_instant(item.timestamp)
    timestamp_value = timestamp.timestamp() if timestamp is not None else float("-inf")
    normalized_text = MemoryModule._normalize_text(item.item.text)
    if item.score is not None:
        return (
            0,
            -float(item.score),
            -timestamp_value,
            item.episode_id or normalized_text,
            item.provider_rank,
            normalized_text,
        )
    return (
        1,
        item.provider_rank,
        -timestamp_value,
        item.episode_id or normalized_text,
        normalized_text,
    )


def _per_leg_rank_key(item: ProviderSearchItem) -> tuple[object, ...]:
    timestamp = _list_timestamp_instant(item.timestamp)
    timestamp_value = timestamp.timestamp() if timestamp is not None else float("-inf")
    normalized_text = MemoryModule._normalize_text(item.item.text)
    return (
        item.provider_rank,
        -timestamp_value,
        item.episode_id or normalized_text,
        normalized_text,
    )


def _profile_text_bytes(value: object) -> bytes | None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return None
    encoded = _utf8_bytes(value)
    if encoded is None:
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
    if encoded is None:
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

    if not isinstance(profile.explicit_info, tuple):
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

    if not isinstance(profile.implicit_traits, tuple):
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
