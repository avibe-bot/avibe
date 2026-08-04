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
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from core.message_output import MessageOutput
from core.runtime_activation import RuntimeActivationIdentity, RuntimeActivationRegistry


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


@dataclass(frozen=True)
class _CompletedOutputEntry:
    """One completion's stable position in a runtime output queue."""

    sequence: int
    queued_at: float
    activity: SessionActivity


@dataclass(frozen=True)
class _ClaimedCompletedOutput:
    entry: _CompletedOutputEntry
    recovered: bool
    externally_retryable: bool = True


def activity_completion_output(
    activity: SessionActivity,
    *,
    activities: list[SessionActivity] | tuple[SessionActivity, ...] | None = None,
    detached: bool,
    completes_turn: bool,
) -> MessageOutput:
    """Build stable Message/Run provenance for one Activity output batch."""

    members = tuple(activities or (activity,))
    if not members:
        members = (activity,)
    batch_id = _activity_output_batch_id(members[0])
    stored_activity_ids = _activity_output_batch_members(members[0])
    activity_ids = (
        stored_activity_ids
        if len(members) == 1 and stored_activity_ids
        else tuple(dict.fromkeys(member.id for member in members))
    )
    run_ids: list[str] = []
    for member in members:
        values = [member.run_id]
        linked = member.metadata.get("run_ids")
        if isinstance(linked, list):
            values.extend(linked)
        for value in values:
            run_id = str(value or "").strip()
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
    stored_run_ids = _activity_output_batch_run_ids(members[0])
    if len(members) == 1 and stored_run_ids:
        run_ids = list(stored_run_ids)

    legacy_native_ids = activity.metadata.get("_legacy_native_message_ids")
    if not isinstance(legacy_native_ids, (list, tuple)):
        legacy_native_ids = ()

    return MessageOutput(
        completes_turn=completes_turn,
        completes_run=True,
        detached=detached,
        idempotency_key=_activity_output_idempotency_key(activity),
        native_message_id_aliases=tuple(
            str(value).strip()
            for value in legacy_native_ids
            if str(value).strip()
        ),
        activity_id=activity.id,
        activity_ids=activity_ids,
        activity_batch_id=batch_id,
        causation_id=activity.parent_activity_id,
        sequence=1,
        run_id=activity.run_id,
        run_ids=tuple(run_ids),
        requires_delivery_for_run_settlement=True,
        metadata={
            "activity_kind": activity.kind,
            "activity_status": activity.status,
            "activity_batch_complete": activity.metadata.get(
                "_output_batch_claim_complete",
                True,
            )
            is not False,
            "activity_local_settlement_only": activity.metadata.get(
                "_output_local_settlement_only",
                False,
            )
            is True,
            "backend": activity.backend,
            "turn_id": activity.turn_id,
        },
    )


def _activity_output_batch_id(activity: SessionActivity) -> str:
    assigned = str(activity.metadata.get("output_batch_id") or "").strip()
    if assigned:
        return assigned
    return f"{activity.backend}:{activity.runtime_key}:activity:{activity.id}"


def _activity_output_batch_members(activity: SessionActivity) -> tuple[str, ...]:
    values = activity.metadata.get("output_batch_activity_ids")
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _activity_output_batch_run_ids(activity: SessionActivity) -> tuple[str, ...]:
    values = activity.metadata.get("output_batch_run_ids")
    if not isinstance(values, list):
        return ()
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _activity_run_id_sequence(activity: SessionActivity) -> tuple[str, ...]:
    values = [activity.run_id]
    linked = activity.metadata.get("run_ids")
    if isinstance(linked, list):
        values.extend(linked)
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value or "").strip())
    )


def _activity_output_idempotency_key(activity: SessionActivity) -> str:
    return f"{activity.backend}-activity-output:{_activity_output_batch_id(activity)}:completion"


def _legacy_activity_output_native_message_id(activity: SessionActivity) -> str:
    key = f"{activity.backend}-task:{activity.runtime_key}:{activity.id}:completion"
    lineage = str(activity.run_id or f"activity:{activity.id}").strip()
    return f"agent-output:{activity.backend or 'unknown'}:{lineage}:{key}"


class SessionActivityRegistry:
    """One shared lifecycle owner for backend-native Activities."""

    def __init__(
        self,
        store: Any = None,
        *,
        activation_registry: RuntimeActivationRegistry | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._store = store
        self._activation_registry = activation_registry
        self._output_settled_callback: Callable[[SessionActivity], None] | None = None
        self._active: dict[tuple[str, str, str], SessionActivity] = {}
        self._active_identities: dict[
            tuple[str, str, str], RuntimeActivationIdentity
        ] = {}
        self._connections: dict[tuple[str, str], tuple[str | None, str]] = {}
        self._connection_identities: dict[
            tuple[str, str], RuntimeActivationIdentity
        ] = {}
        self._completed_outputs: dict[
            tuple[str, str], deque[_CompletedOutputEntry]
        ] = defaultdict(deque)
        self._claimed_completed_outputs: dict[
            tuple[str, str, str], _ClaimedCompletedOutput
        ] = {}
        self._next_completed_output_sequence = 0
        self._recovered_output_ids: set[tuple[str, str, str]] = set()
        self._recovered_terminals: deque[SessionActivity] = deque()
        self._restore()

    def set_output_settled_callback(
        self,
        callback: Callable[[SessionActivity], None] | None,
    ) -> None:
        """Register the runtime wakeup invoked after a completion is acknowledged."""

        with self._lock:
            self._output_settled_callback = callback

    @staticmethod
    def _key(backend: str, runtime_key: str, activity_id: str) -> tuple[str, str, str]:
        return str(backend), str(runtime_key), str(activity_id)

    @classmethod
    def _activity_key(cls, activity: SessionActivity) -> tuple[str, str, str]:
        return cls._key(activity.backend, activity.runtime_key, activity.id)

    def _new_completed_output_entry(
        self,
        activity: SessionActivity,
    ) -> _CompletedOutputEntry:
        self._next_completed_output_sequence += 1
        return _CompletedOutputEntry(
            sequence=self._next_completed_output_sequence,
            queued_at=time.monotonic(),
            activity=activity,
        )

    @staticmethod
    def _activity_run_ids(activity: SessionActivity) -> set[str]:
        return set(_activity_run_id_sequence(activity))

    def _persist_activity(self, activity: SessionActivity, *, phase: str) -> None:
        upsert = getattr(self._store, "upsert_activity", None)
        if not callable(upsert):
            return
        try:
            upsert(activity.to_dict(), phase=phase)
        except Exception:
            logger.warning("Failed to persist Activity %s", activity.id, exc_info=True)
            raise

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
            raise

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
                self._completed_outputs[connection_key].append(
                    self._new_completed_output_entry(activity)
                )
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
        activation_identity: RuntimeActivationIdentity | None = None,
    ) -> bool:
        normalized = state if state in CONNECTION_STATES else "unknown"

        def persist() -> bool:
            key = (str(backend), str(runtime_key))
            with self._lock:
                self._connections[key] = (session_id, normalized)
                self._persist_connection(
                    backend=str(backend),
                    runtime_key=str(runtime_key),
                    session_id=session_id,
                    state=normalized,
                )
                if activation_identity is not None:
                    self._connection_identities[key] = activation_identity
                return True

        return bool(
            self._commit_activation_write(
                backend=backend,
                activation_identity=activation_identity,
                operation="connection",
                commit=persist,
            )
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
        activation_identity: RuntimeActivationIdentity | None = None,
    ) -> SessionActivity | None:
        def upsert() -> SessionActivity:
            with self._lock:
                return self._start_locked(
                    backend=backend,
                    runtime_key=runtime_key,
                    session_id=session_id,
                    activity_id=activity_id,
                    kind=kind,
                    description=description,
                    foreground=foreground,
                    detached_from_run=detached_from_run,
                    parent_activity_id=parent_activity_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    metadata=metadata,
                    activation_identity=activation_identity,
                )

        return self._commit_active_upsert(
            backend=backend,
            activation_identity=activation_identity,
            upsert=upsert,
        )

    def progress(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        activity_id: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        activation_identity: RuntimeActivationIdentity | None = None,
    ) -> SessionActivity | None:
        def upsert() -> SessionActivity:
            key = self._key(backend, runtime_key, activity_id)
            with self._lock:
                existing = self._active.get(key)
                return self._start_locked(
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
                    activation_identity=activation_identity,
                )

        return self._commit_active_upsert(
            backend=backend,
            activation_identity=activation_identity,
            upsert=upsert,
        )

    def _commit_active_upsert(
        self,
        *,
        backend: str,
        activation_identity: RuntimeActivationIdentity | None,
        upsert: Callable[[], SessionActivity],
    ) -> SessionActivity | None:
        return self._commit_activation_write(
            backend=backend,
            activation_identity=activation_identity,
            operation="active Activity upsert",
            commit=upsert,
        )

    def _commit_activation_write(
        self,
        *,
        backend: str,
        activation_identity: RuntimeActivationIdentity | None,
        operation: str,
        commit: Callable[[], Any],
    ) -> Any:
        activation_registry = self._activation_registry
        if activation_registry is None:
            return commit()
        if activation_identity is None:
            logger.error(
                "Refusing %s without runtime activation identity for backend=%s",
                operation,
                backend,
            )
            return None
        if activation_identity.backend != str(backend):
            logger.error(
                "Refusing %s with mismatched runtime backend activity=%s activation=%s",
                operation,
                backend,
                activation_identity.backend,
            )
            return None
        committed = activation_registry.commit_if_current(
            activation_identity,
            commit,
        )
        if not committed.admitted:
            logger.debug(
                "Ignoring stale %s for backend=%s resource=%s generation=%s",
                operation,
                activation_identity.backend,
                activation_identity.resource_key,
                activation_identity.generation,
            )
            return None
        return committed.value

    def _start_locked(
        self,
        *,
        backend: str,
        runtime_key: str,
        session_id: str | None,
        activity_id: str,
        kind: str,
        description: str | None,
        foreground: bool,
        detached_from_run: bool,
        parent_activity_id: str | None,
        turn_id: str | None,
        run_id: str | None,
        metadata: dict[str, Any] | None,
        activation_identity: RuntimeActivationIdentity | None,
    ) -> SessionActivity:
        key = self._key(backend, runtime_key, activity_id)
        now = _now_iso()
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
        self._persist_activity(activity, phase="active")
        self._active[key] = activity
        if activation_identity is not None:
            self._active_identities[key] = activation_identity
        elif self._activation_registry is None:
            self._active_identities.pop(key, None)
        return activity

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
        activation_identity: RuntimeActivationIdentity | None = None,
    ) -> SessionActivity | None:
        key = self._key(backend, runtime_key, activity_id)
        normalized = status if status in TERMINAL_ACTIVITY_STATUSES else "completed"

        def complete_current() -> SessionActivity | None:
            with self._lock:
                if self._activation_registry is not None and (
                    self._active_identities.get(key) is not activation_identity
                ):
                    logger.debug(
                        "Ignoring Activity completion from a non-owning generation "
                        "for backend=%s runtime=%s activity=%s",
                        backend,
                        runtime_key,
                        activity_id,
                    )
                    return None
                return self._complete_locked(
                    key=key,
                    status=normalized,
                    metadata=metadata,
                    expects_output=expects_output,
                    retain_terminal_snapshot=retain_terminal_snapshot,
                )

        return self._commit_activation_write(
            backend=backend,
            activation_identity=activation_identity,
            operation="Activity completion",
            commit=complete_current,
        )

    def _complete_locked(
        self,
        *,
        key: tuple[str, str, str],
        status: str,
        metadata: dict[str, Any] | None,
        expects_output: bool,
        retain_terminal_snapshot: bool,
    ) -> SessionActivity | None:
        backend, runtime_key, _activity_id = key
        now = _now_iso()
        existing = self._active.get(key)
        if existing is None:
            return None
        merged = dict(existing.metadata)
        merged.update(metadata or {})
        completed = replace(
            existing,
            status=status,
            metadata=merged,
            updated_at=now,
            completed_at=now,
        )
        if expects_output:
            self._persist_activity(completed, phase="awaiting_output")
            self._active.pop(key, None)
            self._active_identities.pop(key, None)
            self._completed_outputs[(backend, runtime_key)].append(
                self._new_completed_output_entry(completed)
            )
        elif retain_terminal_snapshot:
            self._persist_activity(completed, phase=TERMINAL_SNAPSHOT_PHASE)
            self._active.pop(key, None)
            self._active_identities.pop(key, None)
        else:
            self._delete_activity(completed)
            self._active.pop(key, None)
            self._active_identities.pop(key, None)
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
        """Claim one receipt and return its canonical (last) batch member."""

        if self.has_claimed_output(backend, runtime_key):
            return None

        claimed = self._claim_completed_outputs(
            backend,
            runtime_key,
            max_age_seconds=max_age_seconds,
            recovered_only=recovered_only,
            limit=1,
        )
        recovered_unbound = False
        if claimed:
            candidate = claimed[0]
            batch_id = str(
                candidate.metadata.get("output_batch_id") or ""
            ).strip()
            recovered_unbound = bool(recovered_only and not batch_id)
            if batch_id:
                claimed.extend(
                    self._claim_completed_outputs(
                        backend,
                        runtime_key,
                        max_age_seconds=max_age_seconds,
                        recovered_only=recovered_only,
                        output_batch_id=batch_id,
                    )
                )
        bound = self._bind_claimed_output_batch_or_requeue(claimed)
        if not bound:
            return None
        representative = bound[-1]
        transient_metadata = dict(representative.metadata)
        batch_run_ids = _activity_output_batch_run_ids(representative)
        if batch_run_ids:
            transient_metadata["run_ids"] = list(batch_run_ids)
        if recovered_unbound:
            transient_metadata["_legacy_native_message_ids"] = [
                _legacy_activity_output_native_message_id(representative)
            ]
        return replace(representative, metadata=transient_metadata)

    def claim_completed_output_batch(
        self,
        backend: str,
        runtime_key: str,
        *,
        turn_ids: set[str] | None = None,
    ) -> list[SessionActivity]:
        """Atomically claim one causal batch without disturbing other output.

        Explicit ``turn_ids`` select every matching completion in FIFO order,
        even when unrelated output is interleaved. Without them, the queue head
        defines the batch; turn-less legacy completions remain single-item.
        """

        key = (str(backend), str(runtime_key))
        with self._lock:
            if any(
                (
                    claimed.entry.activity.backend,
                    claimed.entry.activity.runtime_key,
                )
                == key
                for claimed in self._claimed_completed_outputs.values()
            ):
                return []
            queue = self._completed_outputs.get(key)
            if not queue:
                return []
            identities = (
                {str(turn_id or "").strip() for turn_id in turn_ids}
                if turn_ids is not None
                else None
            )
            if identities is not None:
                identities.discard("")
                if not identities:
                    return []
            candidate = next(
                (
                    entry.activity
                    for entry in queue
                    if identities is None
                    or str(entry.activity.turn_id or "").strip() in identities
                ),
                None,
            )
            if candidate is None:
                return []
            batch_id = str(candidate.metadata.get("output_batch_id") or "").strip()
            if batch_id:
                # A previously bound receipt is an immutable batch boundary. It
                # may be a durable local-only retry and must not absorb a later
                # completion merely because both Activities share one Turn.
                claimed = self._claim_completed_outputs(
                    backend,
                    runtime_key,
                    output_batch_id=batch_id,
                )
            else:
                if identities is None:
                    head_turn_id = str(candidate.turn_id or "").strip()
                    if not head_turn_id:
                        claimed = self._claim_completed_outputs(
                            backend,
                            runtime_key,
                            unbound_only=True,
                            limit=1,
                        )
                        return self._bind_claimed_output_batch_or_requeue(claimed)
                    identities = {head_turn_id}
                claimed = self._claim_completed_outputs(
                    backend,
                    runtime_key,
                    turn_ids=identities,
                    unbound_only=True,
                )
            return self._bind_claimed_output_batch_or_requeue(claimed)

    def _bind_claimed_output_batch_or_requeue(
        self,
        activities: list[SessionActivity],
    ) -> list[SessionActivity]:
        """Bind one complete claim set or atomically restore it for retry."""

        try:
            return self.bind_completed_output_batch(activities)
        except Exception:
            self.requeue_completed_outputs(activities)
            raise

    def bind_completed_output_batch(
        self,
        activities: list[SessionActivity],
    ) -> list[SessionActivity]:
        """Persist one receipt identity onto every claimed batch member."""

        if not activities:
            return []
        assigned_ids = {
            str(activity.metadata.get("output_batch_id") or "").strip()
            for activity in activities
            if str(activity.metadata.get("output_batch_id") or "").strip()
        }
        if len(assigned_ids) > 1:
            raise RuntimeError("Activity output batch members have conflicting receipts")
        if assigned_ids and any(
            not str(activity.metadata.get("output_batch_id") or "").strip()
            for activity in activities
        ):
            raise RuntimeError("Activity output batch members have conflicting receipts")
        batch_id = (
            next(iter(assigned_ids))
            if assigned_ids
            else (
                f"{activities[0].backend}:{activities[0].runtime_key}:"
                f"batch:{uuid.uuid4().hex}"
            )
        )
        raw_member_lists: list[tuple[str, ...]] = []
        for activity in activities:
            values = activity.metadata.get("output_batch_activity_ids")
            if values is None:
                continue
            if not isinstance(values, list):
                raise RuntimeError(
                    "Activity output batch members have conflicting membership"
                )
            normalized = tuple(
                str(value).strip() for value in values if str(value).strip()
            )
            if len(normalized) != len(set(normalized)):
                raise RuntimeError(
                    "Activity output batch members have conflicting membership"
                )
            raw_member_lists.append(normalized)
        persisted_member_sets = {
            members for members in raw_member_lists if members
        }
        if len(persisted_member_sets) > 1:
            raise RuntimeError("Activity output batch members have conflicting membership")
        if assigned_ids and len(raw_member_lists) != len(activities):
            raise RuntimeError("Activity output batch persisted membership is incomplete")
        activity_ids = (
            next(iter(persisted_member_sets))
            if persisted_member_sets
            else tuple(dict.fromkeys(activity.id for activity in activities))
        )
        claim_complete = True
        if persisted_member_sets:
            claimed_ids = tuple(activity.id for activity in activities)
            if len(claimed_ids) != len(set(claimed_ids)) or not set(
                claimed_ids
            ).issubset(activity_ids):
                raise RuntimeError(
                    "Activity output batch members have conflicting membership"
                )
            claim_complete = len(claimed_ids) == len(activity_ids)
            activity_by_id = {activity.id: activity for activity in activities}
            activities = [
                activity_by_id[activity_id]
                for activity_id in activity_ids
                if activity_id in activity_by_id
            ]
        persisted_run_sets = {
            _activity_output_batch_run_ids(activity)
            for activity in activities
            if _activity_output_batch_run_ids(activity)
        }
        if len(persisted_run_sets) > 1:
            raise RuntimeError("Activity output batch members have conflicting Run provenance")
        if persisted_run_sets:
            run_ids = next(iter(persisted_run_sets))
        else:
            ordered_run_ids: list[str] = []
            for activity in activities:
                for run_id in _activity_run_id_sequence(activity):
                    if run_id not in ordered_run_ids:
                        ordered_run_ids.append(run_id)
            run_ids = tuple(ordered_run_ids)

        updates: list[tuple[tuple[str, str, str], SessionActivity]] = []
        local_only_by_key: dict[tuple[str, str, str], bool] = {}
        persisted_current_by_key: dict[
            tuple[str, str, str], SessionActivity
        ] = {}
        with self._lock:
            for activity in activities:
                activity_key = self._activity_key(activity)
                claimed = self._claimed_completed_outputs.get(activity_key)
                if claimed is None:
                    continue
                current = claimed.entry.activity
                persistent_metadata = dict(current.metadata)
                persistent_metadata.pop("_output_batch_claim_complete", None)
                local_only_by_key[activity_key] = bool(
                    persistent_metadata.pop("_output_local_settlement_only", False)
                )
                persistent_metadata.pop("_legacy_native_message_ids", None)
                persisted_current_by_key[activity_key] = replace(
                    current,
                    metadata=persistent_metadata,
                )
                updated = replace(
                    current,
                    metadata={
                        **persistent_metadata,
                        "output_batch_id": batch_id,
                        "output_batch_activity_ids": list(activity_ids),
                        "output_batch_run_ids": list(run_ids),
                    },
                )
                updates.append((activity_key, updated))

            if len(updates) != len(activities):
                raise RuntimeError("Activity output batch is not completely claimed")

            changed = [
                updated
                for activity_key, updated in updates
                if updated
                != persisted_current_by_key[activity_key]
            ]
            if changed:
                if len(updates) == 1:
                    self._persist_activity(changed[0], phase="awaiting_output")
                else:
                    persist_batch = getattr(self._store, "upsert_activities", None)
                    persist_one = getattr(self._store, "upsert_activity", None)
                    if callable(persist_batch):
                        try:
                            persist_batch(
                                [updated.to_dict() for _key, updated in updates],
                                phase="awaiting_output",
                            )
                        except Exception:
                            logger.warning(
                                "Failed to persist Activity output batch %s",
                                batch_id,
                                exc_info=True,
                            )
                            raise
                    elif callable(persist_one):
                        raise RuntimeError(
                            "Durable Activity store cannot atomically bind an output batch"
                        )

            published_updates = []
            for activity_key, updated in updates:
                published = replace(
                    updated,
                    metadata={
                        **updated.metadata,
                        "_output_batch_claim_complete": claim_complete,
                        **(
                            {"_output_local_settlement_only": True}
                            if local_only_by_key[activity_key]
                            else {}
                        ),
                    },
                )
                claimed = self._claimed_completed_outputs[activity_key]
                self._claimed_completed_outputs[activity_key] = replace(
                    claimed,
                    entry=replace(claimed.entry, activity=published),
                )
                published_updates.append(published)
        return published_updates

    def claimed_completed_output_for_idempotency_key(
        self,
        idempotency_key: str,
    ) -> SessionActivity | None:
        """Return the claimed Activity owning one stable output identity."""

        identity = str(idempotency_key or "").strip()
        if not identity:
            return None
        with self._lock:
            for claimed in self._claimed_completed_outputs.values():
                activity = claimed.entry.activity
                if _activity_output_idempotency_key(activity) == identity:
                    return activity
        return None

    def claimed_completed_output_batch_for_output(
        self,
        output: MessageOutput,
    ) -> list[SessionActivity]:
        """Return the ordered claimed set protected by one output receipt."""

        activity_ids = set(output.activity_ids)
        if output.activity_id:
            activity_ids.add(output.activity_id)
        receipt_id = str(output.idempotency_key or "").strip()
        with self._lock:
            claimed = sorted(
                self._claimed_completed_outputs.values(),
                key=lambda item: item.entry.sequence,
            )
            if receipt_id:
                matched = [
                    item.entry.activity
                    for item in claimed
                    if _activity_output_idempotency_key(item.entry.activity)
                    == receipt_id
                ]
                if matched:
                    ordered_ids = tuple(output.activity_ids)
                    if ordered_ids and len(matched) == len(ordered_ids):
                        matched_by_id = {activity.id: activity for activity in matched}
                        if set(matched_by_id) == set(ordered_ids):
                            return [
                                matched_by_id[activity_id]
                                for activity_id in ordered_ids
                            ]
                    return matched
            return [
                item.entry.activity
                for item in claimed
                if item.entry.activity.id in activity_ids
            ]

    def _claim_completed_outputs(
        self,
        backend: str,
        runtime_key: str,
        *,
        turn_ids: set[str] | None = None,
        max_age_seconds: float = 0,
        recovered_only: bool = False,
        limit: int | None = None,
        output_batch_id: str | None = None,
        unbound_only: bool = False,
    ) -> list[SessionActivity]:
        key = (str(backend), str(runtime_key))
        identities = (
            {str(turn_id or "").strip() for turn_id in turn_ids}
            if turn_ids is not None
            else None
        )
        if identities is not None:
            identities.discard("")
            if not identities:
                return []
        now = time.monotonic()
        with self._lock:
            queue = self._completed_outputs.get(key)
            if not queue:
                return []
            retained: deque[_CompletedOutputEntry] = deque()
            claimed: list[SessionActivity] = []
            while queue:
                entry = queue.popleft()
                if limit is not None and len(claimed) >= limit:
                    retained.append(entry)
                    retained.extend(queue)
                    break
                activity = entry.activity
                assigned_batch_id = str(
                    activity.metadata.get("output_batch_id") or ""
                ).strip()
                if output_batch_id is not None and assigned_batch_id != output_batch_id:
                    retained.append(entry)
                    continue
                if unbound_only and assigned_batch_id:
                    retained.append(entry)
                    continue
                activity_key = self._activity_key(activity)
                is_recovered = activity_key in self._recovered_output_ids
                if recovered_only and not is_recovered:
                    retained.append(entry)
                    continue
                if max_age_seconds > 0 and now - entry.queued_at > max_age_seconds:
                    try:
                        self._delete_activity(activity)
                    except Exception:
                        retained.append(entry)
                        retained.extend(queue)
                        self._completed_outputs[key] = retained
                        raise
                    self._recovered_output_ids.discard(activity_key)
                    continue
                turn_id = str(activity.turn_id or "").strip()
                if identities is not None and turn_id not in identities:
                    retained.append(entry)
                    continue
                self._claimed_completed_outputs[activity_key] = _ClaimedCompletedOutput(
                    entry=entry,
                    recovered=is_recovered,
                    externally_retryable=not bool(
                        activity.metadata.get("_output_local_settlement_only")
                    ),
                )
                self._recovered_output_ids.discard(activity_key)
                claimed.append(activity)
            if retained:
                self._completed_outputs[key] = retained
            else:
                self._completed_outputs.pop(key, None)
            return claimed

    def requeue_completed_outputs(
        self,
        activities: list[SessionActivity],
    ) -> int:
        """Restore a claimed batch to its original positions atomically."""

        restored_by_runtime: dict[
            tuple[str, str], list[_ClaimedCompletedOutput]
        ] = defaultdict(list)
        with self._lock:
            for activity in activities:
                activity_key = self._activity_key(activity)
                claimed = self._claimed_completed_outputs.get(activity_key)
                if claimed is None:
                    continue
                if not claimed.externally_retryable:
                    continue
                self._claimed_completed_outputs.pop(activity_key, None)
                key = (str(activity.backend), str(activity.runtime_key))
                restored_by_runtime[key].append(claimed)
                if claimed.recovered:
                    self._recovered_output_ids.add(activity_key)
            for key, restored in restored_by_runtime.items():
                entries = list(self._completed_outputs.get(key) or ())
                entries.extend(item.entry for item in restored)
                entries.sort(key=lambda item: item.sequence)
                self._completed_outputs[key] = deque(entries)
        return sum(len(items) for items in restored_by_runtime.values())

    def requeue_completed_outputs_for_local_settlement(
        self,
        activities: list[SessionActivity],
        *,
        recovered: bool | None = None,
    ) -> int:
        """Requeue durable terminal claims without making them externally sendable."""

        if not activities:
            return 0
        restored_by_runtime: dict[
            tuple[str, str], list[_ClaimedCompletedOutput]
        ] = defaultdict(list)
        with self._lock:
            claimed_items: list[
                tuple[tuple[str, str, str], _ClaimedCompletedOutput]
            ] = []
            for activity in activities:
                activity_key = self._activity_key(activity)
                claimed = self._claimed_completed_outputs.get(activity_key)
                if (
                    claimed is None
                    or claimed.externally_retryable
                    or not claimed.entry.activity.metadata.get(
                        "_output_local_settlement_only"
                    )
                ):
                    return 0
                claimed_items.append((activity_key, claimed))

            for activity_key, claimed in claimed_items:
                self._claimed_completed_outputs.pop(activity_key, None)
                activity = claimed.entry.activity
                key = (str(activity.backend), str(activity.runtime_key))
                restored_by_runtime[key].append(claimed)
                if claimed.recovered if recovered is None else recovered:
                    self._recovered_output_ids.add(activity_key)
            for key, restored in restored_by_runtime.items():
                entries = list(self._completed_outputs.get(key) or ())
                entries.extend(item.entry for item in restored)
                entries.sort(key=lambda item: item.sequence)
                self._completed_outputs[key] = deque(entries)
        return len(claimed_items)

    def requeue_completed_output(
        self,
        activity: SessionActivity,
        *,
        front: bool = True,
        recovered: bool | None = None,
    ) -> bool:
        """Restore one claim or its complete receipt group for retry."""

        key = (str(activity.backend), str(activity.runtime_key))
        activity_key = self._activity_key(activity)
        with self._lock:
            claimed = self._claimed_completed_outputs.get(activity_key)
            if claimed is None:
                return False
            batch_id = str(
                claimed.entry.activity.metadata.get("output_batch_id") or ""
            ).strip()
            if batch_id:
                batch = sorted(
                    (
                        item
                        for item in self._claimed_completed_outputs.values()
                        if str(
                            item.entry.activity.metadata.get("output_batch_id") or ""
                        ).strip()
                        == batch_id
                    ),
                    key=lambda item: item.entry.sequence,
                )
                if batch and any(not item.externally_retryable for item in batch):
                    local_only = all(
                        item.entry.activity.metadata.get(
                            "_output_local_settlement_only"
                        )
                        is True
                        for item in batch
                    )
                    if not local_only:
                        return False
                    return self.requeue_completed_outputs_for_local_settlement(
                        [item.entry.activity for item in batch],
                        recovered=recovered,
                    ) == len(batch)
                if len(batch) > 1:
                    return self.requeue_completed_outputs(
                        [item.entry.activity for item in batch]
                    ) == len(batch)
            if not claimed.externally_retryable:
                if claimed.entry.activity.metadata.get(
                    "_output_local_settlement_only"
                ) is True:
                    return self.requeue_completed_outputs_for_local_settlement(
                        [claimed.entry.activity],
                        recovered=recovered,
                    ) == 1
                return False
            self._claimed_completed_outputs.pop(activity_key, None)
            if recovered is None:
                recovered = claimed.recovered
            queue = self._completed_outputs[key]
            if front:
                lowest_sequence = min(
                    (entry.sequence for entry in queue),
                    default=claimed.entry.sequence + 1,
                )
                queue.appendleft(
                    replace(
                        claimed.entry,
                        sequence=lowest_sequence - 1,
                        queued_at=time.monotonic(),
                    )
                )
            else:
                queue.append(self._new_completed_output_entry(activity))
            if recovered:
                self._recovered_output_ids.add(activity_key)
        return True

    @staticmethod
    def delivered_output_failure(
        activity: SessionActivity,
        error: BaseException,
    ) -> SessionActivity:
        return replace(
            activity,
            status="failed",
            metadata={
                **activity.metadata,
                "terminal_error": (
                    "delivered_output_local_settlement_failed: "
                    f"{str(error).strip() or type(error).__name__}"
                ),
            },
        )

    def settle_completed_output_batch(
        self,
        output: MessageOutput,
        *,
        accepted_message_exists: bool,
        settlement_error: BaseException | None = None,
        settle_terminal: Callable[[SessionActivity], bool] | None = None,
        terminal_activities: tuple[SessionActivity, ...] | None = None,
        visible_output: bool = True,
    ) -> bool:
        """Settle every claimed member under its explicit visibility policy.

        A visible claim needs accepted-Message or terminal-Activity evidence.
        Invisible output has no transport to repeat, so successful local settlement
        may release it even when terminal snapshot cleanup fails. Run-settlement
        callbacks are invoked outside the Registry lock and never count as durable
        anti-redelivery evidence.
        """

        activities = self.claimed_completed_output_batch_for_output(output)
        if not activities:
            return False
        terminal_by_id = {
            activity.id: activity for activity in (terminal_activities or ())
        }
        if settlement_error is not None:
            terminal_by_id.update(
                {
                    activity.id: self.delivered_output_failure(
                        activity,
                        settlement_error,
                    )
                    for activity in activities
                    if activity.id not in terminal_by_id
                }
            )

        evidence_safe: dict[tuple[str, str, str], bool] = {}
        claimed_by_key: dict[
            tuple[str, str, str], _ClaimedCompletedOutput
        ] = {}
        with self._lock:
            for activity in activities:
                activity_key = self._activity_key(activity)
                claimed = self._claimed_completed_outputs.get(activity_key)
                if claimed is None:
                    continue
                if visible_output:
                    claimed = replace(claimed, externally_retryable=False)
                terminal_activity = terminal_by_id.get(activity.id)
                if terminal_activity is not None:
                    claimed = replace(
                        claimed,
                        entry=replace(claimed.entry, activity=terminal_activity),
                    )
                self._claimed_completed_outputs[activity_key] = claimed
                claimed_by_key[activity_key] = claimed

                persisted = False
                try:
                    if terminal_activity is not None:
                        self._persist_activity(
                            terminal_activity,
                            phase=TERMINAL_SNAPSHOT_PHASE,
                        )
                    else:
                        self._delete_activity(activity)
                    persisted = True
                except Exception:
                    if terminal_activity is None:
                        try:
                            self._persist_activity(
                                activity,
                                phase=TERMINAL_SNAPSHOT_PHASE,
                            )
                            persisted = True
                        except Exception:
                            pass
                    if not persisted and visible_output:
                        logger.error(
                            "Delivered Activity lacks local terminal evidence "
                            "(activity=%s accepted_message=%s)",
                            activity.id,
                            accepted_message_exists,
                            exc_info=True,
                        )
                    elif not persisted:
                        logger.warning(
                            "Invisible Activity terminal evidence cleanup failed after "
                            "local settlement (activity=%s)",
                            activity.id,
                            exc_info=True,
                        )
                evidence_safe[activity_key] = bool(
                    persisted or accepted_message_exists or not visible_output
                )
                if (
                    terminal_activity is not None
                    and persisted
                    and not accepted_message_exists
                ):
                    claimed = self._claimed_completed_outputs[activity_key]
                    local_only_activity = replace(
                        claimed.entry.activity,
                        metadata={
                            **claimed.entry.activity.metadata,
                            "_output_local_settlement_only": True,
                        },
                    )
                    claimed = replace(
                        claimed,
                        entry=replace(
                            claimed.entry,
                            activity=local_only_activity,
                        ),
                    )
                    self._claimed_completed_outputs[activity_key] = claimed
                    claimed_by_key[activity_key] = claimed

        terminal_settled = True
        if terminal_by_id:
            terminal_settled = settle_terminal is not None
            if settle_terminal is not None:
                for activity in activities:
                    terminal_activity = terminal_by_id.get(activity.id)
                    if terminal_activity is None:
                        continue
                    try:
                        if not settle_terminal(terminal_activity):
                            terminal_settled = False
                    except Exception:
                        terminal_settled = False
                        logger.error(
                            "Failed to settle delivered Activity terminal state "
                            "(activity=%s)",
                            activity.id,
                            exc_info=True,
                        )

        if not terminal_settled:
            return False

        released: list[SessionActivity] = []
        with self._lock:
            if len(claimed_by_key) != len(activities) or not all(
                evidence_safe.values()
            ):
                return False
            for activity in activities:
                activity_key = self._activity_key(activity)
                claimed = claimed_by_key.get(activity_key)
                if self._claimed_completed_outputs.get(activity_key) is not claimed:
                    return False
            for activity in activities:
                activity_key = self._activity_key(activity)
                self._claimed_completed_outputs.pop(activity_key, None)
                self._recovered_output_ids.discard(activity_key)
                released.append(activity)
            callback = self._output_settled_callback

        if callback is not None:
            for activity in released:
                try:
                    callback(activity)
                except Exception:
                    logger.warning(
                        "Failed to signal settled Activity output %s",
                        activity.id,
                        exc_info=True,
                    )
        return len(released) == len(activities)

    def settle_completed_output_delivery(
        self,
        activity: SessionActivity,
        *,
        accepted_message_exists: bool,
        terminal_activity: SessionActivity | None = None,
        settle_terminal: Callable[[SessionActivity], bool] | None = None,
        visible_output: bool = True,
    ) -> bool:
        """Single-member adapter for non-batched Registry callers."""

        output = activity_completion_output(
            activity,
            detached=True,
            completes_turn=False,
        )
        return self.settle_completed_output_batch(
            output,
            accepted_message_exists=accepted_message_exists,
            settlement_error=(
                RuntimeError("delivered output terminal settlement")
                if terminal_activity is not None
                else None
            ),
            settle_terminal=settle_terminal,
            terminal_activities=(terminal_activity,) if terminal_activity else None,
            visible_output=visible_output,
        )

    def ack_completed_output(self, activity: SessionActivity) -> bool:
        """Settle a recovered completion after its recovery owner handled it."""

        with self._lock:
            claimed = self._claimed_completed_outputs.get(
                self._activity_key(activity)
            )
            recovered = bool(claimed and claimed.recovered)

        return self.settle_completed_output_delivery(
            activity,
            accepted_message_exists=False,
            # Visible recovery is consumed by the dispatcher before this legacy
            # adapter runs. A still-claimed recovered output was settled without
            # transport by ScheduledTaskService and needs no delivery receipt.
            visible_output=not recovered,
        )

    def has_completed_output(self, backend: str, runtime_key: str) -> bool:
        """Whether a completed Activity is waiting for user-visible output."""

        with self._lock:
            prefix = (str(backend), str(runtime_key))
            return bool(self._completed_outputs.get(prefix)) or any(
                (claimed.entry.activity.backend, claimed.entry.activity.runtime_key) == prefix
                for claimed in self._claimed_completed_outputs.values()
            )

    def has_claimed_output(self, backend: str, runtime_key: str) -> bool:
        """Whether one batch already owns transport/local settlement."""

        key = (str(backend), str(runtime_key))
        with self._lock:
            return any(
                (
                    claimed.entry.activity.backend,
                    claimed.entry.activity.runtime_key,
                )
                == key
                for claimed in self._claimed_completed_outputs.values()
            )

    def has_pending_run_output(self, run_id: str) -> bool:
        identity = str(run_id or "").strip()
        if not identity:
            return False
        with self._lock:
            pending = [
                entry.activity
                for queue in self._completed_outputs.values()
                for entry in queue
            ]
            pending.extend(
                claimed.entry.activity
                for claimed in self._claimed_completed_outputs.values()
                if claimed.externally_retryable
            )
            return any(identity in self._activity_run_ids(activity) for activity in pending)

    def recovered_output_runtimes(self) -> list[tuple[str, str]]:
        """Runtime queues containing completion output restored after restart."""

        with self._lock:
            runtimes = {
                (entry.activity.backend, entry.activity.runtime_key)
                for queue in self._completed_outputs.values()
                for entry in queue
                if self._activity_key(entry.activity) in self._recovered_output_ids
            }
        return sorted(runtimes)

    def recovered_output_delay_seconds(
        self,
        backend: str,
        runtime_key: str,
        *,
        grace_seconds: float,
        now: datetime | None = None,
    ) -> float | None:
        """Return the durable remaining live-batching grace for one runtime."""

        key = (str(backend), str(runtime_key))
        instant = now or datetime.now(timezone.utc)
        with self._lock:
            recovered = [
                entry.activity
                for entry in self._completed_outputs.get(key, ())
                if self._activity_key(entry.activity) in self._recovered_output_ids
            ]
        if not recovered:
            return None
        if any(
            str(activity.metadata.get("output_batch_id") or "").strip()
            or activity.metadata.get("_output_local_settlement_only")
            for activity in recovered
        ):
            return 0.0
        completed: list[datetime] = []
        for activity in recovered:
            raw = str(activity.completed_at or "").strip()
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            completed.append(parsed.astimezone(timezone.utc))
        first = min(completed)
        elapsed = max(0.0, (instant - first).total_seconds())
        return max(0.0, float(grace_seconds) - elapsed)

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
            return (
                any(key[0] == identity for key in self._active)
                or any(
                    key[0] == identity and bool(queue)
                    for key, queue in self._completed_outputs.items()
                )
                or any(key[0] == identity for key in self._claimed_completed_outputs)
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
            runtime_keys.update(
                runtime_key
                for item_backend, runtime_key, _activity_id in self._claimed_completed_outputs
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
                    force=True,
                )
            )
        now = _now_iso()
        with self._lock:
            pending_by_key = {
                self._activity_key(entry.activity): entry.activity
                for key, queue in self._completed_outputs.items()
                if key[0] == identity
                for entry in queue
            }
            pending_by_key.update(
                {
                    key: claimed.entry.activity
                    for key, claimed in self._claimed_completed_outputs.items()
                    if key[0] == identity
                }
            )
            terminated_pending = [
                replace(
                    activity,
                    status=status if status in TERMINAL_ACTIVITY_STATUSES else "killed",
                    updated_at=now,
                    completed_at=now,
                )
                for activity in pending_by_key.values()
            ]
            for activity in terminated_pending:
                self._persist_activity(activity, phase=TERMINAL_SNAPSHOT_PHASE)
            for key in [key for key in self._completed_outputs if key[0] == identity]:
                self._completed_outputs.pop(key, None)
            for key in [key for key in self._claimed_completed_outputs if key[0] == identity]:
                self._claimed_completed_outputs.pop(key, None)
            for activity in terminated_pending:
                self._recovered_output_ids.discard(self._activity_key(activity))
        completed.extend(terminated_pending)
        return completed

    def end_runtime(
        self,
        backend: str,
        runtime_key: str,
        *,
        status: str = "disconnected",
        retain_terminal_snapshots: bool = False,
        activation_identity: RuntimeActivationIdentity | None = None,
        force: bool = False,
    ) -> list[SessionActivity]:
        key = (str(backend), str(runtime_key))
        if (
            self._activation_registry is not None
            and activation_identity is None
            and not force
        ):
            logger.error(
                "Refusing Activity runtime teardown without exact activation identity "
                "for backend=%s runtime=%s",
                backend,
                runtime_key,
            )
            return []
        with self._lock:
            connection = self._connections.get(key)
            active_items = [
                (activity_key, activity)
                for activity_key, activity in self._active.items()
                if activity_key[:2] == key
                and (
                    force
                    or self._activation_registry is None
                    or self._active_identities.get(activity_key) is activation_identity
                )
            ]
            session_id = connection[0] if connection else None
            if session_id is None:
                session_id = next(
                    (item.session_id for _activity_key, item in active_items if item.session_id),
                    None,
                )
            connection_owned = bool(
                force
                or self._activation_registry is None
                or self._connection_identities.get(key) is activation_identity
            )
            if connection_owned:
                normalized_state = (
                    status if status in CONNECTION_STATES else "disconnected"
                )
                self._connections[key] = (session_id, normalized_state)
                self._persist_connection(
                    backend=str(backend),
                    runtime_key=str(runtime_key),
                    session_id=session_id,
                    state=normalized_state,
                )
                self._connection_identities.pop(key, None)

            completed: list[SessionActivity] = []
            for activity_key, _activity in active_items:
                activity = self._complete_locked(
                    key=activity_key,
                    status=(
                        status
                        if status in TERMINAL_ACTIVITY_STATUSES
                        else "disconnected"
                    ),
                    metadata=None,
                    expects_output=False,
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
                for entry in queue
                if entry.activity.session_id == session_id
            ) + sum(
                1
                for claimed in self._claimed_completed_outputs.values()
                if claimed.entry.activity.session_id == session_id
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
