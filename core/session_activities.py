"""Backend-neutral, process-local Activity lifecycle registry.

Activities are operational state: they answer what work is alive independently
from foreground Turn ownership. Durable Messages and Harness Runs retain their
own persistence aggregates.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


TERMINAL_ACTIVITY_STATUSES = frozenset({"completed", "failed", "stopped", "killed", "disconnected"})
CONNECTION_STATES = frozenset({"connected", "reconnecting", "disconnected", "unknown"})


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


class SessionActivityRegistry:
    """One shared lifecycle owner for backend-native Activities."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: dict[tuple[str, str, str], SessionActivity] = {}
        self._connections: dict[tuple[str, str], tuple[str | None, str]] = {}
        self._completed_outputs: dict[
            tuple[str, str], deque[tuple[float, SessionActivity]]
        ] = defaultdict(deque)

    @staticmethod
    def _key(backend: str, runtime_key: str, activity_id: str) -> tuple[str, str, str]:
        return str(backend), str(runtime_key), str(activity_id)

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
        max_age_seconds: float = 300.0,
    ) -> SessionActivity | None:
        key = (str(backend), str(runtime_key))
        now = time.monotonic()
        with self._lock:
            queue = self._completed_outputs.get(key)
            if not queue:
                return None
            while queue:
                completed_at, activity = queue.popleft()
                if max_age_seconds <= 0 or now - completed_at <= max_age_seconds:
                    if not queue:
                        self._completed_outputs.pop(key, None)
                    return activity
            self._completed_outputs.pop(key, None)
        return None

    def end_runtime(self, backend: str, runtime_key: str, *, status: str = "disconnected") -> None:
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
            active_ids = [activity.id for activity in active]
            self._completed_outputs.pop(key, None)
        for activity_id in active_ids:
            self.complete(
                backend=backend,
                runtime_key=runtime_key,
                activity_id=activity_id,
                status="disconnected",
            )

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
            "connection": connection,
        }
