"""Optional-package-owned best-effort capture facade."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from avibe_memory.admission import (
    CaptureAdmission,
    InboundTurnFacts,
    PrincipalDirectory,
    normalize_attachment_config_generation,
)
from avibe_memory.im_attachments import select_memory_attachments
from avibe_memory.types import CaptureAccepted, CaptureRequest, CaptureSkipped
from core.attachment_telemetry import log_attachment_capture
from core.blocking import run_blocking
from core.memory_adapter import MemoryEvent, SessionArchived, SessionReset, TurnAccepted


logger = logging.getLogger(__name__)
MAX_PENDING_CAPTURE_EVENTS = 256


def _record_attachment_capture(platform: str, total: int, captured: int) -> None:
    try:
        log_attachment_capture(platform, total, captured)
    except BaseException:
        logger.debug("Memory attachment capture telemetry failed", exc_info=True)


class CaptureModule(Protocol):
    def reserve_capture_capacity(self) -> object: ...

    def release_capture_capacity(self, reservation: object) -> None: ...

    def reserve_capture_admission(
        self,
        *,
        principal_id: str,
        project_id: str,
        session_id: str,
    ) -> object: ...

    def cancel_capture_reservation(self, reservation: object) -> None: ...

    def capture_admission(
        self,
        *,
        principal_id: str,
        project_id: str,
        session_id: str,
        reservation: object,
    ) -> Any: ...

    async def capture(
        self,
        request: CaptureRequest,
        *,
        source_lease: object = None,
        admission: object = None,
        capacity_reservation: object = None,
    ) -> object: ...

    def offer_barrier(self, session_id: str) -> object: ...

    async def wait_writer_idle_for_tests(self, *, timeout_seconds: float = 5.0) -> None: ...


class _UserBindings:
    def __init__(self, is_enabled_user: Callable[[str, str], bool]) -> None:
        self._is_enabled_user = is_enabled_user

    def is_enabled_user(self, platform: str, user_id: str) -> bool:
        return self._is_enabled_user(platform, user_id)


@dataclass(slots=True)
class _QueuedTurn:
    event: TurnAccepted
    module: CaptureModule
    capacity: object
    lease: object | None
    active: bool = True
    task_owned: bool = False
    attachment_capture_recorded: bool = False

    def record_attachment_capture(self, captured: int = 0) -> None:
        if self.attachment_capture_recorded:
            return
        event = self.event
        if event.platform == "avibe" or not event.files:
            return
        self.attachment_capture_recorded = True
        _record_attachment_capture(
            str(event.platform),
            len(event.files),
            captured,
        )

    def release(self) -> None:
        if not self.active:
            return
        self.record_attachment_capture()
        self.active = False
        _release(self.lease)
        try:
            self.module.release_capture_capacity(self.capacity)
        except BaseException:
            pass


@dataclass(slots=True)
class _CaptureOwnership:
    item: _QueuedTurn
    reservation: object
    active: bool = True

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            self.item.module.cancel_capture_reservation(self.reservation)
        except BaseException:
            pass
        self.item.release()


class EnabledMemoryAdapter:
    """Own capture admission, preparation, scheduling, and cleanup."""

    def __init__(
        self,
        *,
        module: CaptureModule | None,
        principals: PrincipalDirectory,
        is_enabled_user: Callable[[str, str], bool],
        lifecycle_snapshot_matches: Callable[[str, object], bool],
        acquire_lifecycle_admission: Callable[[str], Awaitable[object]],
        attachment_capture_status: Callable[[], Awaitable[str]],
        attachment_config_generation: Callable[[], int | None],
        attachment_selector: Callable[[object], object] = select_memory_attachments,
        max_pending_events: int = MAX_PENDING_CAPTURE_EVENTS,
    ) -> None:
        self._module = module
        self._admission = CaptureAdmission(
            principals=principals,
            bindings=_UserBindings(is_enabled_user),
        )
        self._lifecycle_snapshot_matches = lifecycle_snapshot_matches
        self._acquire_lifecycle_admission = acquire_lifecycle_admission
        self._attachment_capture_status = attachment_capture_status
        self._attachment_config_generation = attachment_config_generation
        self._attachment_selector = attachment_selector
        self._task_factory: Callable[..., asyncio.Task[Any]] | None = None
        self._queue: asyncio.Queue[MemoryEvent | _QueuedTurn] = asyncio.Queue(
            maxsize=max_pending_events
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._capture_tasks: set[asyncio.Task[None]] = set()
        self._capture_tasks_by_session: dict[str, set[asyncio.Task[None]]] = {}
        self._registration_open = False

    def bind_module(self, module: CaptureModule) -> None:
        """Bind the recovered store once without changing facade identity."""

        if self._module is not None and self._module is not module:
            raise RuntimeError("Memory capture facade is already bound")
        self._module = module

    @property
    def capture_tasks(self) -> set[asyncio.Task[None]]:
        return set(self._capture_tasks)

    def start(
        self,
        *,
        task_factory: Callable[..., asyncio.Task[Any]] | None = None,
    ) -> bool:
        """Schedule the sole preparation worker before the host can offer work."""

        if task_factory is not None:
            if self._task_factory is not None and self._task_factory != task_factory:
                raise RuntimeError("Memory capture scheduler is already bound")
            self._task_factory = task_factory
        factory = self._task_factory
        if self._module is None or factory is None:
            return False
        if self._worker_task is not None and not self._worker_task.done():
            return True
        self._registration_open = True
        pending = self._run()
        try:
            self._worker_task = factory(
                pending,
                name="memory-capture-dispatcher",
            )
        except BaseException:
            pending.close()
            self._registration_open = False
            return False
        return True

    def offer(self, event: MemoryEvent, /) -> None:
        """Retain and enqueue using only closed facts and in-memory decisions."""

        queued: MemoryEvent | _QueuedTurn | None = None
        try:
            worker = self._worker_task
            module = self._module
            if (
                not self._registration_open
                or module is None
                or worker is None
                or worker.done()
            ):
                return
            if isinstance(event, TurnAccepted):
                if not _valid_turn(event):
                    return
                capacity = module.reserve_capture_capacity()
                if isinstance(capacity, str):
                    if event.platform != "avibe" and event.files:
                        _record_attachment_capture(
                            str(event.platform),
                            len(event.files),
                            0,
                        )
                    return
                lease = None
                if event.attachment_lease is not None:
                    try:
                        lease = event.attachment_lease.retain()
                    except BaseException:
                        lease = None
                queued = _QueuedTurn(event, module, capacity, lease)
            elif isinstance(event, (SessionReset, SessionArchived)):
                if not isinstance(event.session_id, str) or not event.session_id:
                    return
                queued = event
            else:
                return
            self._queue.put_nowait(queued)
            queued = None
        except BaseException:
            if isinstance(queued, _QueuedTurn):
                queued.release()

    async def _run(self) -> None:
        current: _QueuedTurn | None = None
        try:
            while True:
                item = await self._queue.get()
                try:
                    if isinstance(item, _QueuedTurn):
                        current = item
                        await self._prepare_and_schedule(item)
                        current = None
                    else:
                        module = self._module
                        if module is not None:
                            module.offer_barrier(item.session_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Memory capture preparation failed", exc_info=True)
                    if isinstance(item, _QueuedTurn):
                        item.release()
                        current = None
                finally:
                    self._queue.task_done()
        finally:
            if current is not None and not current.task_owned:
                current.release()

    async def _prepare_and_schedule(self, item: _QueuedTurn) -> None:
        event = item.event
        if not self._matches(event):
            item.release()
            return
        facts = InboundTurnFacts(
            platform=event.platform,
            user_id=event.user_id,
            message_id=event.message_id,
            session_id=event.session_id,
            text=event.text,
            files=list(event.files),
            is_dm=event.is_dm,
            is_ordinary_text=event.is_ordinary_text,
            is_ordinary_attachment=event.is_ordinary_attachment,
            attachment_lease=item.lease,
            attachment_capture_status="unavailable",
            attachment_config_generation=None,
            attachment_selection=None,
            sender_name=event.sender_name,
        )
        if event.platform != "avibe" and event.files:
            authorized = await run_blocking(
                self._admission.admits_attachment_turn,
                facts,
            )
            if not authorized:
                item.release()
                return
        attachment_status: object = "unavailable"
        generation: object = None
        attachment_selection: object = None
        if event.platform != "avibe" and event.files and item.lease is not None:
            try:
                generation = normalize_attachment_config_generation(
                    self._attachment_config_generation()
                )
            except Exception:
                generation = None
            if generation is not None:
                try:
                    attachment_status = await self._attachment_capture_status()
                except Exception:
                    attachment_status = "unavailable"
            if attachment_status == "ready":
                try:
                    attachment_selection = await run_blocking(
                        self._attachment_selector,
                        item.lease,
                    )
                except Exception as error:
                    logger.warning(
                        "memory_attachment_selection_failed "
                        "platform=%s count=%d error_type=%s",
                        event.platform,
                        len(event.files),
                        type(error).__name__,
                    )
                    attachment_status = "unavailable"
                    generation = None
        facts = replace(
            facts,
            attachment_capture_status=attachment_status,
            attachment_config_generation=generation,
            attachment_selection=attachment_selection,
        )
        decision = await run_blocking(self._admission.decide, facts)
        if isinstance(decision, CaptureSkipped) or not self._matches(event):
            item.release()
            return
        request = self._validate_attachment_generation(decision)
        if request is None:
            item.release()
            return
        try:
            reservation = item.module.reserve_capture_admission(
                principal_id=request.principal_id,
                project_id=request.project_id,
                session_id=request.session_id,
            )
        except BaseException:
            item.release()
            return
        ownership = _CaptureOwnership(item, reservation)
        pending = self._run_capture(item, request, ownership)
        task: asyncio.Task[Any] | None = None
        try:
            factory = self._task_factory
            if factory is None:
                raise RuntimeError("Memory capture scheduler is not bound")
            task = factory(pending, name="memory-capture")
            item.task_owned = True
            self._track_capture_task(task, request.session_id, ownership)
        except BaseException:
            if task is None:
                pending.close()
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            ownership.release()
            return

    def _validate_attachment_generation(
        self,
        request: CaptureRequest,
    ) -> CaptureRequest | None:
        if not request.attachments:
            return request
        try:
            observed = normalize_attachment_config_generation(
                self._attachment_config_generation()
            )
        except Exception:
            observed = None
        if observed == request.attachment_config_generation:
            return request
        if not request.text.strip():
            return None
        return replace(
            request,
            attachments=(),
            attachment_config_generation=None,
        )

    async def _run_capture(
        self,
        item: _QueuedTurn,
        request: CaptureRequest,
        ownership: _CaptureOwnership,
    ) -> None:
        lifecycle_admission = None
        try:
            lifecycle_admission = await self._acquire_lifecycle_admission(
                request.session_id
            )
            if not self._registration_open or not self._matches(item.event):
                return
            async with item.module.capture_admission(
                principal_id=request.principal_id,
                project_id=request.project_id,
                session_id=request.session_id,
                reservation=ownership.reservation,
            ) as admission:
                if not self._registration_open or not self._matches(item.event):
                    return
                result = await item.module.capture(
                    request,
                    source_lease=item.lease,
                    admission=admission,
                    capacity_reservation=item.capacity,
                )
                if item.event.platform != "avibe" and item.event.files:
                    captured = (
                        result.captured_attachment_count
                        if isinstance(result, CaptureAccepted)
                        else 0
                    )
                    item.record_attachment_capture(captured)
        finally:
            _release(lifecycle_admission)
            ownership.release()

    def _matches(self, event: TurnAccepted) -> bool:
        try:
            return self._lifecycle_snapshot_matches(
                event.session_id,
                event.lifecycle_snapshot,
            )
        except Exception:
            return False

    def _track_capture_task(
        self,
        task: asyncio.Task[None],
        session_id: str,
        ownership: _CaptureOwnership,
    ) -> None:
        if not self._registration_open:
            task.cancel()
        self._capture_tasks.add(task)
        self._capture_tasks_by_session.setdefault(session_id, set()).add(task)

        def done(completed: asyncio.Task[None]) -> None:
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Memory capture task failed", exc_info=True)
            ownership.release()
            self._capture_tasks.discard(completed)
            bucket = self._capture_tasks_by_session.get(session_id)
            if bucket is not None:
                bucket.discard(completed)
                if not bucket:
                    self._capture_tasks_by_session.pop(session_id, None)

        task.add_done_callback(done)

    def abandon_memory_captures_for_session(self, session_id: str) -> None:
        for task in tuple(self._capture_tasks_by_session.get(session_id, ())):
            task.cancel()

    def quiesce_memory_capture_tasks(self) -> None:
        self._registration_open = False

    def cancel_memory_capture_tasks_nowait(self) -> None:
        worker = self._worker_task
        if worker is not None:
            worker.cancel()
        for task in tuple(self._capture_tasks):
            task.cancel()

    async def cancel_memory_capture_tasks(self) -> None:
        self.quiesce_memory_capture_tasks()
        worker = self._worker_task
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self._worker_task = None
        tasks = tuple(self._capture_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _QueuedTurn):
                item.release()
            self._queue.task_done()

    async def wait_idle_for_tests(self, *, timeout_seconds: float = 5.0) -> None:
        async def wait() -> None:
            await self._queue.join()
            while self._capture_tasks:
                await asyncio.sleep(0)
            module = self._module
            if module is not None:
                await module.wait_writer_idle_for_tests(
                    timeout_seconds=timeout_seconds
                )

        await asyncio.wait_for(wait(), timeout=timeout_seconds)


def _valid_turn(event: TurnAccepted) -> bool:
    return bool(
        isinstance(event.session_id, str)
        and event.session_id
        and isinstance(event.text, str)
        and isinstance(event.files, tuple)
    )


def _release(resource: object) -> None:
    release = getattr(resource, "release", None)
    if not callable(release):
        return
    try:
        release()
    except BaseException:
        pass
