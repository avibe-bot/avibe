"""Host-owned capture boundary for optional Memory integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnAccepted:
    """One committed human turn offered for best-effort capture."""

    context: object
    text: str
    session_id: str
    lifecycle_snapshot: object
    attachment_lease: object | None = None


@dataclass(frozen=True, slots=True)
class SessionReset:
    """A core session generation was reset successfully."""

    session_id: str


@dataclass(frozen=True, slots=True)
class SessionArchived:
    """A core session was archived successfully."""

    session_id: str


MemoryEvent: TypeAlias = TurnAccepted | SessionReset | SessionArchived


class MemoryCaptureAdapter(Protocol):
    """Accept a best-effort host event without waiting or raising."""

    def offer(self, event: MemoryEvent, /) -> None: ...


class DisabledMemoryAdapter:
    """No-op capture target used while Memory is disabled."""

    def offer(self, event: MemoryEvent, /) -> None:
        del event


class EnabledMemoryAdapter:
    """Own best-effort capture admission and tasks for one Controller."""

    def __init__(self, controller: object) -> None:
        self._controller = controller
        self._capture_tasks: set[asyncio.Task[Any]] = set()
        self._capture_tasks_by_session: dict[str, set[asyncio.Task[Any]]] = {}
        self._registration_open = True

    @property
    def capture_tasks(self) -> set[asyncio.Task[Any]]:
        return set(self._capture_tasks)

    def offer(self, event: MemoryEvent, /) -> None:
        """Validate, reserve, and register without waiting or leaking errors."""

        try:
            if isinstance(event, TurnAccepted):
                self._offer_turn(event)
            elif isinstance(event, (SessionReset, SessionArchived)):
                runtime = getattr(self._controller, "memory_runtime", None)
                barrier = getattr(runtime, "offer_barrier", None)
                if callable(barrier):
                    barrier(event.session_id)
        except BaseException:
            try:
                logger.warning("Memory event offer failed", exc_info=True)
            except BaseException:
                pass

    def _offer_turn(self, event: TurnAccepted) -> None:
        if not self._registration_open:
            return
        manager = getattr(self._controller, "session_turns", None)
        matches = getattr(manager, "session_lifecycle_snapshot_matches", None)
        if callable(matches) and not matches(
            event.session_id,
            event.lifecycle_snapshot,
        ):
            return
        reservation = None
        retained_lease = None
        capture: Awaitable[None] | None = None
        try:
            is_attachment = event.attachment_lease is not None
            reserve_name = (
                "reserve_memory_attachment_capture"
                if is_attachment
                else "reserve_memory_capture_capacity"
            )
            reserve = getattr(self._controller, reserve_name, None)
            if callable(reserve):
                reservation = (
                    reserve(event.context, event.session_id)
                    if is_attachment
                    else reserve(event.context, event.text, event.session_id)
                )
            if getattr(
                reservation,
                "capacity_blocked",
                getattr(reservation, "capacity_full", False),
            ):
                self._release(reservation)
                return

            options: dict[str, object] = {}
            if is_attachment:
                options["attachment_reservation"] = reservation
                generation = self._normalize_generation(
                    getattr(reservation, "config_generation", None)
                )
                options["attachment_config_generation"] = generation
                if generation is not None:
                    try:
                        retained_lease = event.attachment_lease.retain()
                    except BaseException as error:
                        options["attachment_text_only"] = True
                        try:
                            logger.warning(
                                "Memory attachment lease could not be retained; "
                                "capturing text only error_type=%s",
                                type(error).__name__,
                            )
                        except BaseException:
                            pass
                    else:
                        options["attachment_lease"] = retained_lease
            elif reservation is not None:
                options["attachment_reservation"] = reservation

            capture_memory = getattr(self._controller, "capture_user_memory", None)
            if not callable(capture_memory) or not event.text.strip():
                self._release(retained_lease)
                self._release(reservation)
                return
            capture = capture_memory(
                event.context,
                event.text,
                event.session_id,
                **options,
            )
            if not self._schedule_capture(
                event,
                capture,
                attachment_lease=retained_lease,
                reservation=reservation,
            ):
                capture = None
        except BaseException:
            self._close_capture(capture)
            self._release(retained_lease)
            self._release(reservation)
            raise

    def _schedule_capture(
        self,
        event: TurnAccepted,
        capture: Awaitable[None],
        *,
        attachment_lease: object,
        reservation: object,
    ) -> bool:
        if not self._registration_open:
            self._close_capture(capture)
            self._release(attachment_lease)
            self._release(reservation)
            return False
        try:
            task = asyncio.create_task(
                self._run_capture(event, capture),
                name="memory-capture",
            )
        except BaseException:
            self._close_capture(capture)
            self._release(attachment_lease)
            self._release(reservation)
            return False
        self._track_capture_task(
            task,
            session_id=event.session_id,
            attachment_lease=attachment_lease,
            reservation=reservation,
            capture=capture,
        )
        return True

    async def _run_capture(
        self,
        event: TurnAccepted,
        capture: Awaitable[None],
    ) -> None:
        pending: Awaitable[None] | None = capture
        admission = None
        try:
            if not self._registration_open:
                return
            manager = getattr(self._controller, "session_turns", None)
            acquire = getattr(manager, "acquire_lifecycle_admission", None)
            if callable(acquire):
                admission = await acquire(event.session_id)
            if not self._registration_open:
                return
            matches = getattr(manager, "session_lifecycle_snapshot_matches", None)
            if callable(matches) and not matches(
                event.session_id,
                event.lifecycle_snapshot,
            ):
                return
            await capture
            pending = None
        finally:
            self._release(admission)
            if pending is not None:
                self._close_capture(pending)

    def _track_capture_task(
        self,
        task: asyncio.Task[Any],
        *,
        session_id: str,
        attachment_lease: object = None,
        reservation: object = None,
        capture: object = None,
    ) -> None:
        self._capture_tasks.add(task)
        self._capture_tasks_by_session.setdefault(session_id, set()).add(task)

        def on_done(done_task: asyncio.Task[Any]) -> None:
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Memory capture task failed", exc_info=True)
            finally:
                self._close_capture(capture)
                self._release(attachment_lease)
                self._release(reservation)
                self._capture_tasks.discard(done_task)
                bucket = self._capture_tasks_by_session.get(session_id)
                if bucket is not None:
                    bucket.discard(done_task)
                    if not bucket:
                        self._capture_tasks_by_session.pop(session_id, None)

        task.add_done_callback(on_done)

    def abandon_memory_captures_for_session(self, session_id: str) -> None:
        for task in tuple(self._capture_tasks_by_session.get(session_id, ())):
            task.cancel()

    def quiesce_memory_capture_tasks(self) -> None:
        self._registration_open = False

    def cancel_memory_capture_tasks_nowait(self) -> None:
        for task in tuple(self._capture_tasks):
            task.cancel()

    async def cancel_memory_capture_tasks(self) -> None:
        while self._capture_tasks:
            tasks = tuple(self._capture_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._capture_tasks.difference_update(tasks)

    @staticmethod
    def _normalize_generation(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _release(resource: object) -> None:
        release = getattr(resource, "release", None)
        if not callable(release):
            return
        try:
            release()
        except BaseException:
            try:
                logger.debug("Memory capture resource release failed", exc_info=True)
            except BaseException:
                pass

    @staticmethod
    def _close_capture(capture: object) -> None:
        close = getattr(capture, "close", None)
        if not callable(close):
            return
        try:
            close()
        except BaseException:
            try:
                logger.debug("Memory capture coroutine close failed", exc_info=True)
            except BaseException:
                pass
