"""Backend-neutral Activity lifecycle registry.

Activities are operational state: they answer what work is alive independently
from foreground Turn ownership. The registry can persist restart snapshots in
the existing runtime-record aggregate; durable Messages and Harness Runs retain
their own persistence aggregates.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from core.message_output import MessageOutput


logger = logging.getLogger(__name__)


TERMINAL_ACTIVITY_STATUSES = frozenset({"completed", "failed", "stopped", "killed", "disconnected"})
CONNECTION_STATES = frozenset({"connected", "reconnecting", "disconnected", "unknown"})
TERMINAL_SNAPSHOT_PHASE = "terminal"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SessionActivity:
    id: str
    backend: str
    runtime_key: str
    session_id: str | None
    kind: str
    status: str = "running"
    description: str | None = None
    foreground: bool = False
    detached_from_run: bool = False
    parent_activity_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backend": self.backend,
            "runtime_key": self.runtime_key,
            "session_id": self.session_id,
            "kind": self.kind,
            "status": self.status,
            "description": self.description,
            "foreground": self.foreground,
            "detached_from_run": self.detached_from_run,
            "parent_activity_id": self.parent_activity_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "metadata": dict(self.metadata),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionActivity":
        return cls(
            id=str(payload.get("id") or ""),
            backend=str(payload.get("backend") or ""),
            runtime_key=str(payload.get("runtime_key") or ""),
            session_id=(str(payload["session_id"]) if payload.get("session_id") else None),
            kind=str(payload.get("kind") or "background_task"),
            status=str(payload.get("status") or "running"),
            description=(str(payload["description"]) if payload.get("description") else None),
            foreground=bool(payload.get("foreground")),
            detached_from_run=bool(payload.get("detached_from_run")),
            parent_activity_id=(
                str(payload["parent_activity_id"])
                if payload.get("parent_activity_id")
                else None
            ),
            turn_id=str(payload["turn_id"]) if payload.get("turn_id") else None,
            run_id=str(payload["run_id"]) if payload.get("run_id") else None,
            metadata=(dict(payload["metadata"]) if isinstance(payload.get("metadata"), dict) else {}),
            started_at=str(payload.get("started_at") or _now_iso()),
            updated_at=str(payload.get("updated_at") or _now_iso()),
            completed_at=(str(payload["completed_at"]) if payload.get("completed_at") else None),
        )


def activity_completion_output(
    activity: SessionActivity,
    *,
    detached: bool,
    completes_turn: bool,
) -> MessageOutput:
    """Build stable Message/Run provenance for one Activity completion."""

    return MessageOutput(
        completes_turn=completes_turn,
        completes_run=True,
        detached=detached,
        idempotency_key=(
            f"{activity.backend}-task:{activity.runtime_key}:{activity.id}:completion"
        ),
        activity_id=activity.id,
        causation_id=activity.parent_activity_id,
        sequence=1,
        run_id=activity.run_id,
        metadata={
            "activity_kind": activity.kind,
            "activity_status": activity.status,
            "backend": activity.backend,
            "run_ids": activity.metadata.get("run_ids"),
            "turn_id": activity.turn_id,
        },
    )


class SessionActivityRegistry:
    """One shared lifecycle owner for backend-native Activities."""

    def __init__(self, store: Any = None) -> None:
        self._lock = threading.RLock()
        self._store = store
        self._active: dict[tuple[str, str, str], SessionActivity] = {}
        self._connections: dict[tuple[str, str], tuple[str | None, str]] = {}
        self._completed_outputs: dict[
            tuple[str, str], deque[tuple[float, SessionActivity]]
        ] = defaultdict(deque)
        self._claimed_completed_outputs: dict[
            tuple[str, str, str], tuple[SessionActivity, bool]
        ] = {}
        self._recovered_output_ids: set[tuple[str, str, str]] = set()
        self._recovered_terminals: deque[SessionActivity] = deque()
        self._restore()

    @staticmethod
    def _key(backend: str, runtime_key: str, activity_id: str) -> tuple[str, str, str]:
        return str(backend), str(runtime_key), str(activity_id)

    @classmethod
    def _activity_key(cls, activity: SessionActivity) -> tuple[str, str, str]:
        return cls._key(activity.backend, activity.runtime_key, activity.id)

    @staticmethod
    def _activity_run_ids(activity: SessionActivity) -> set[str]:
        run_ids = {str(activity.run_id)} if activity.run_id else set()
        values = activity.metadata.get("run_ids")
        if isinstance(values, list):
            run_ids.update(str(value) for value in values if str(value or "").strip())
        return run_ids

    def _persist_activity(self, activity: SessionActivity, *, phase: str) -> None:
        upsert = getattr(self._store, "upsert_activity", None)
        if not callable(upsert):
            return
        try:
            upsert(activity.to_dict(), phase=phase)
        except Exception:
            logger.warning("Failed to persist Activity %s", activity.id, exc_info=True)

    def _delete_activity(self, activity: SessionActivity) -> None:
        delete = getattr(self._store, "delete_activity", None)
        if not callable(delete):
            return
        try:
            delete(
                backend=activity.backend,
                runtime_key=activity.runtime_key,
                activity_id=activity.id,
            )
        except Exception:
            logger.warning("Failed to delete Activity snapshot %s", activity.id, exc_info=True)

    def _persist_connection(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        state: str,
    ) -> None:
        upsert = getattr(self._store, "upsert_connection", None)
        if not callable(upsert):
            return
        try:
            upsert(
                backend=backend,
                runtime_key=runtime_key,
                session_id=session_id,
                state=state,
            )
        except Exception:
            logger.warning(
                "Failed to persist %s Activity connection %s",
                backend,
                runtime_key,
                exc_info=True,
            )

    def _restore(self) -> None:
        list_connections = getattr(self._store, "list_connections", None)
        if callable(list_connections):
            try:
                connections = list_connections()
            except Exception:
                connections = []
                logger.warning("Failed to restore Activity connections", exc_info=True)
            for payload in connections:
                backend = str(payload.get("backend") or "")
                runtime_key = str(payload.get("runtime_key") or "")
                if not backend or not runtime_key:
                    continue
                session_id = str(payload["session_id"]) if payload.get("session_id") else None
                self._connections[(backend, runtime_key)] = (session_id, "disconnected")
                self._persist_connection(
                    backend=backend,
                    runtime_key=runtime_key,
                    session_id=session_id,
                    state="disconnected",
                )

        list_activities = getattr(self._store, "list_activities", None)
        if not callable(list_activities):
            return
        try:
            records = list_activities()
        except Exception:
            logger.warning("Failed to restore Activities", exc_info=True)
            return
        now = _now_iso()
        for record in records:
            raw_activity = record.get("activity")
            if not isinstance(raw_activity, dict):
                continue
            activity = SessionActivity.from_dict(raw_activity)
            if not activity.id or not activity.backend or not activity.runtime_key:
                continue
            key = self._activity_key(activity)
            connection_key = (activity.backend, activity.runtime_key)
            self._connections[connection_key] = (activity.session_id, "disconnected")
            self._persist_connection(
                backend=activity.backend,
                runtime_key=activity.runtime_key,
                session_id=activity.session_id,
                state="disconnected",
            )
            phase = record.get("phase")
            if phase == "awaiting_output":
                self._completed_outputs[connection_key].append((time.monotonic(), activity))
                self._recovered_output_ids.add(key)
                continue

            recovered = (
                activity
                if phase == TERMINAL_SNAPSHOT_PHASE
                else replace(
                    activity,
                    status="disconnected",
                    updated_at=now,
                    completed_at=now,
                )
            )
            self._recovered_terminals.append(recovered)

    def set_connection(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        state: str,
    ) -> None:
        normalized = state if state in CONNECTION_STATES else "unknown"
        with self._lock:
            self._connections[(str(backend), str(runtime_key))] = (session_id, normalized)
            self._persist_connection(
                backend=str(backend),
                runtime_key=str(runtime_key),
                session_id=session_id,
                state=normalized,
            )

    def start(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        activity_id: str,
        kind: str,
        description: str | None = None,
        foreground: bool = False,
        detached_from_run: bool = False,
        parent_activity_id: str | None = None,
        turn_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionActivity:
        key = self._key(backend, runtime_key, activity_id)
        now = _now_iso()
        with self._lock:
            existing = self._active.get(key)
            if existing is None:
                activity = SessionActivity(
                    id=str(activity_id),
                    backend=str(backend),
                    runtime_key=str(runtime_key),
                    session_id=session_id,
                    kind=str(kind),
                    description=description,
                    foreground=foreground,
                    detached_from_run=detached_from_run,
                    parent_activity_id=parent_activity_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    metadata=dict(metadata or {}),
                    started_at=now,
                    updated_at=now,
                )
            else:
                merged = dict(existing.metadata)
                merged.update(metadata or {})
                activity = replace(
                    existing,
                    session_id=session_id or existing.session_id,
                    kind=str(kind or existing.kind),
                    status="running",
                    description=description or existing.description,
                    foreground=foreground,
                    detached_from_run=detached_from_run,
                    parent_activity_id=parent_activity_id or existing.parent_activity_id,
                    turn_id=turn_id or existing.turn_id,
                    run_id=run_id or existing.run_id,
                    metadata=merged,
                    updated_at=now,
                    completed_at=None,
                )
            self._active[key] = activity
            self._persist_activity(activity, phase="active")
            return activity

    def progress(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        activity_id: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionActivity:
        key = self._key(backend, runtime_key, activity_id)
        with self._lock:
            existing = self._active.get(key)
        return self.start(
            backend=backend,
            runtime_key=runtime_key,
            session_id=session_id or (existing.session_id if existing else None),
            activity_id=activity_id,
            kind=existing.kind if existing else "background_task",
            description=description or (existing.description if existing else None),
            foreground=existing.foreground if existing else False,
            detached_from_run=existing.detached_from_run if existing else False,
            parent_activity_id=existing.parent_activity_id if existing else None,
            turn_id=existing.turn_id if existing else None,
            run_id=existing.run_id if existing else None,
            metadata=metadata,
        )

    def complete(
        self,
        *,
        backend: str,
        runtime_key: str,
        activity_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
        expects_output: bool = False,
        retain_terminal_snapshot: bool = False,
    ) -> SessionActivity | None:
        key = self._key(backend, runtime_key, activity_id)
        normalized = status if status in TERMINAL_ACTIVITY_STATUSES else "completed"
        now = _now_iso()
        with self._lock:
            existing = self._active.pop(key, None)
            if existing is None:
                return None
            merged = dict(existing.metadata)
            merged.update(metadata or {})
            completed = replace(
                existing,
                status=normalized,
                metadata=merged,
                updated_at=now,
                completed_at=now,
            )
            if expects_output:
                self._completed_outputs[(str(backend), str(runtime_key))].append(
                    (time.monotonic(), completed)
                )
                self._persist_activity(completed, phase="awaiting_output")
            elif retain_terminal_snapshot:
                self._persist_activity(completed, phase=TERMINAL_SNAPSHOT_PHASE)
            else:
                self._delete_activity(completed)
            return completed

    def active_for_runtime(self, backend: str, runtime_key: str) -> list[SessionActivity]:
        prefix = (str(backend), str(runtime_key))
        with self._lock:
            values = [
                activity
                for (item_backend, item_runtime, _), activity in self._active.items()
                if (item_backend, item_runtime) == prefix
            ]
        return sorted(values, key=lambda item: (item.started_at, item.id))

    def has_active(self, backend: str, runtime_key: str) -> bool:
        return bool(self.active_for_runtime(backend, runtime_key))

    def has_blocking_run_activity(self, run_id: str) -> bool:
        """Whether a non-detached active Activity is owned by ``run_id``."""

        identity = str(run_id or "").strip()
        if not identity:
            return False
        with self._lock:
            for activity in self._active.values():
                run_ids = activity.metadata.get("run_ids")
                owns_run = activity.run_id == identity or (
                    isinstance(run_ids, list) and identity in {str(item) for item in run_ids}
                )
                if owns_run and not activity.detached_from_run:
                    return True
        return False

    def claim_completed_output(
        self,
        backend: str,
        runtime_key: str,
        *,
        max_age_seconds: float = 0,
        recovered_only: bool = False,
    ) -> SessionActivity | None:
        key = (str(backend), str(runtime_key))
        now = time.monotonic()
        with self._lock:
            queue = self._completed_outputs.get(key)
            if not queue:
                return None
            candidates = len(queue)
            while queue and candidates > 0:
                candidates -= 1
                completed_at, activity = queue.popleft()
                activity_key = self._activity_key(activity)
                is_recovered = activity_key in self._recovered_output_ids
                if recovered_only and not is_recovered:
                    queue.append((completed_at, activity))
                    continue
                if max_age_seconds <= 0 or now - completed_at <= max_age_seconds:
                    self._claimed_completed_outputs[activity_key] = (activity, is_recovered)
                    self._recovered_output_ids.discard(activity_key)
                    if not queue:
                        self._completed_outputs.pop(key, None)
                    return activity
                self._recovered_output_ids.discard(activity_key)
                self._delete_activity(activity)
            if not queue:
                self._completed_outputs.pop(key, None)
        return None

    def requeue_completed_output(
        self,
        activity: SessionActivity,
        *,
        front: bool = True,
        recovered: bool | None = None,
    ) -> None:
        """Restore a claimed completion when its causal output cannot be consumed yet."""

        key = (str(activity.backend), str(activity.runtime_key))
        activity_key = self._activity_key(activity)
        item = (time.monotonic(), activity)
        with self._lock:
            claimed = self._claimed_completed_outputs.pop(activity_key, None)
            if recovered is None:
                recovered = claimed[1] if claimed is not None else False
            queue = self._completed_outputs[key]
            if front:
                queue.appendleft(item)
            else:
                queue.append(item)
            if recovered:
                self._recovered_output_ids.add(activity_key)
            self._persist_activity(activity, phase="awaiting_output")

    def ack_completed_output(self, activity: SessionActivity) -> None:
        """Forget a durable completion only after its output policy succeeds."""

        activity_key = self._activity_key(activity)
        with self._lock:
            self._claimed_completed_outputs.pop(activity_key, None)
            self._recovered_output_ids.discard(activity_key)
            self._delete_activity(activity)

    def has_completed_output(self, backend: str, runtime_key: str) -> bool:
        """Whether a completed Activity is waiting for user-visible output."""

        with self._lock:
            prefix = (str(backend), str(runtime_key))
            return bool(self._completed_outputs.get(prefix)) or any(
                (item.backend, item.runtime_key) == prefix
                for item, _recovered in self._claimed_completed_outputs.values()
            )

    def has_pending_run_output(self, run_id: str) -> bool:
        identity = str(run_id or "").strip()
        if not identity:
            return False
        with self._lock:
            pending = [
                activity
                for queue in self._completed_outputs.values()
                for _created_at, activity in queue
            ]
            pending.extend(activity for activity, _recovered in self._claimed_completed_outputs.values())
            return any(identity in self._activity_run_ids(activity) for activity in pending)

    def recovered_output_runtimes(self) -> list[tuple[str, str]]:
        """Runtime queues containing completion output restored after restart."""

        with self._lock:
            runtimes = {
                (activity.backend, activity.runtime_key)
                for queue in self._completed_outputs.values()
                for _created_at, activity in queue
                if self._activity_key(activity) in self._recovered_output_ids
            }
        return sorted(runtimes)

    def drain_recovered_terminals(self) -> list[SessionActivity]:
        with self._lock:
            values = list(self._recovered_terminals)
            self._recovered_terminals.clear()
        return values

    def ack_recovered_terminal(self, activity: SessionActivity) -> None:
        """Delete a recovered live snapshot only after its Run policy settles."""

        with self._lock:
            self._delete_activity(activity)

    def has_backend_work(self, backend: str) -> bool:
        """Whether a backend has live Activities or undelivered completions."""

        identity = str(backend)
        with self._lock:
            return any(key[0] == identity for key in self._active) or any(
                key[0] == identity and bool(queue)
                for key, queue in self._completed_outputs.items()
            )

    def end_backend(self, backend: str, *, status: str = "killed") -> list[SessionActivity]:
        """Settle every Activity owned by a force-terminated backend runtime."""

        identity = str(backend)
        with self._lock:
            runtime_keys = {
                runtime_key
                for item_backend, runtime_key, _activity_id in self._active
                if item_backend == identity
            }
            runtime_keys.update(
                runtime_key
                for item_backend, runtime_key in self._connections
                if item_backend == identity
            )
            runtime_keys.update(
                runtime_key
                for item_backend, runtime_key in self._completed_outputs
                if item_backend == identity
            )
        completed: list[SessionActivity] = []
        for runtime_key in runtime_keys:
            completed.extend(
                self.end_runtime(
                    identity,
                    runtime_key,
                    status=status,
                    retain_terminal_snapshots=True,
                )
            )
        with self._lock:
            pending = [
                activity
                for key, queue in self._completed_outputs.items()
                if key[0] == identity
                for _completed_at, activity in queue
            ]
            for key in [key for key in self._completed_outputs if key[0] == identity]:
                self._completed_outputs.pop(key, None)
        now = _now_iso()
        terminated_pending = [
            replace(
                activity,
                status=status if status in TERMINAL_ACTIVITY_STATUSES else "killed",
                updated_at=now,
                completed_at=now,
            )
            for activity in pending
        ]
        with self._lock:
            for activity in terminated_pending:
                self._recovered_output_ids.discard(self._activity_key(activity))
                self._persist_activity(activity, phase=TERMINAL_SNAPSHOT_PHASE)
        completed.extend(terminated_pending)
        return completed

    def end_runtime(
        self,
        backend: str,
        runtime_key: str,
        *,
        status: str = "disconnected",
        retain_terminal_snapshots: bool = False,
    ) -> list[SessionActivity]:
        key = (str(backend), str(runtime_key))
        with self._lock:
            connection = self._connections.get(key)
            active = [
                activity
                for (item_backend, item_runtime, _), activity in self._active.items()
                if (item_backend, item_runtime) == key
            ]
            session_id = connection[0] if connection else None
            if session_id is None:
                session_id = next((item.session_id for item in active if item.session_id), None)
            self._connections[key] = (
                session_id,
                status if status in CONNECTION_STATES else "disconnected",
            )
            self._persist_connection(
                backend=str(backend),
                runtime_key=str(runtime_key),
                session_id=session_id,
                state=status if status in CONNECTION_STATES else "disconnected",
            )
            active_ids = [activity.id for activity in active]
        completed: list[SessionActivity] = []
        for activity_id in active_ids:
            activity = self.complete(
                backend=backend,
                runtime_key=runtime_key,
                activity_id=activity_id,
                status=status if status in TERMINAL_ACTIVITY_STATUSES else "disconnected",
                retain_terminal_snapshot=retain_terminal_snapshots,
            )
            if activity is not None:
                completed.append(activity)
        return completed

    def session_state(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            activities = sorted(
                (
                    activity
                    for activity in self._active.values()
                    if activity.session_id == session_id and not activity.foreground
                ),
                key=lambda item: (item.started_at, item.id),
            )
            connection_states = [
                state
                for connection_session_id, state in self._connections.values()
                if connection_session_id == session_id
            ]
            pending_output_count = sum(
                1
                for queue in self._completed_outputs.values()
                for _created_at, activity in queue
                if activity.session_id == session_id
            ) + sum(
                1
                for activity, _recovered in self._claimed_completed_outputs.values()
                if activity.session_id == session_id
            )
        if "connected" in connection_states:
            connection = "connected"
        elif "reconnecting" in connection_states:
            connection = "reconnecting"
        elif connection_states and all(state == "disconnected" for state in connection_states):
            connection = "disconnected"
        else:
            connection = "unknown"
        return {
            "background_activities": [activity.to_dict() for activity in activities],
            "pending_activity_output_count": pending_output_count,
            "connection": connection,
        }
