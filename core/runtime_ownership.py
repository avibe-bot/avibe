"""Read-only durable ownership snapshots for backend runtime resources."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from sqlalchemy import Engine, and_, func, or_, select

from storage.background import (
    EXECUTION_RUN_TYPES,
    RUN_STATUS_ALIASES,
    TERMINAL_RUN_STATUSES,
    normalize_run_status,
)
from storage.delivery_states import DELIVERY_STATE_MATRIX, policy_for
from storage.models import (
    agent_runs,
    agent_sessions,
    agents,
    message_deliveries,
    runtime_records,
    session_turns,
)
from storage.session_activities import (
    ACTIVE_PHASE,
    ACTIVITY_RECORD_TYPE,
    AWAITING_OUTPUT_PHASE,
)

logger = logging.getLogger(__name__)


class SessionRuntimeDisposition(str, Enum):
    ACTIVE = "active"
    TRANSITIONING = "transitioning"
    RUNNABLE = "runnable"
    WAITING = "waiting"
    RECLAIMABLE = "reclaimable"
    UNKNOWN = "unknown"


_DISPOSITION_PRIORITY = {
    SessionRuntimeDisposition.RECLAIMABLE: 0,
    SessionRuntimeDisposition.WAITING: 1,
    SessionRuntimeDisposition.RUNNABLE: 2,
    SessionRuntimeDisposition.TRANSITIONING: 3,
    SessionRuntimeDisposition.ACTIVE: 4,
    SessionRuntimeDisposition.UNKNOWN: 5,
}

_KNOWN_ACTIVITY_PHASES = frozenset(
    {ACTIVE_PHASE, AWAITING_OUTPUT_PHASE, "terminal"}
)
_TERMINAL_RAW_RUN_STATUSES = tuple(
    raw
    for raw, normalized in RUN_STATUS_ALIASES.items()
    if normalized in TERMINAL_RUN_STATUSES
)
_TERMINAL_DELIVERY_STATES = tuple(
    state
    for state, policy in DELIVERY_STATE_MATRIX.items()
    if policy.ordering == "terminal"
)


@dataclass(frozen=True)
class RuntimeSessionBinding:
    """Exact adapter-provided durable identities for one runtime Session."""

    session_id: str
    session_anchor: str
    workdir: str | None
    activity_runtime_keys: tuple[str, ...]
    fallback_route_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeResourceTarget:
    """One disposable backend resource and the exact durable keys it serves."""

    backend: str
    resource_key: str
    bindings: tuple[RuntimeSessionBinding, ...] = ()
    known_activity_runtime_keys: tuple[str, ...] = ()
    known_fallback_route_keys: tuple[str, ...] = ()
    durable_session_workdir: str | None = None
    include_all_backend_sessions: bool = False
    maps_all_backend_activities: bool = False
    maps_all_backend_fallback_runs: bool = False


@dataclass(frozen=True)
class SessionRuntimeOwnershipSnapshot:
    session_id: str
    disposition: SessionRuntimeDisposition
    delivery_ids: tuple[str, ...]
    turn_ids: tuple[str, ...]
    active_activity_ids: tuple[str, ...]
    fallback_run_ids: tuple[str, ...]
    queue_hold_state: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeTargetOwnershipSnapshot:
    backend: str
    resource_key: str
    activity_runtime_keys: tuple[str, ...]
    sessions: tuple[SessionRuntimeOwnershipSnapshot, ...]
    sessionless_active_activity_ids: tuple[str, ...]
    sessionless_fallback_run_ids: tuple[str, ...]
    disposition: SessionRuntimeDisposition
    reasons: tuple[str, ...] = ()

    @property
    def blocks_reclamation(self) -> bool:
        return self.disposition in {
            SessionRuntimeDisposition.ACTIVE,
            SessionRuntimeDisposition.TRANSITIONING,
            SessionRuntimeDisposition.UNKNOWN,
        }

    @property
    def blocks_transport_replacement(self) -> bool:
        """Whether durable native-effect evidence vetoes transport replacement.

        Pre-native Delivery and Turn rows use the adapter's live-turn fence.
        This gate covers evidence that can outlive that local fence: unknown
        ownership, active Activity effects, and fallback Runs with a live PID.
        """

        if self.disposition is SessionRuntimeDisposition.UNKNOWN:
            return True
        if self.sessionless_active_activity_ids or any(
            session.active_activity_ids for session in self.sessions
        ):
            return True
        for session in self.sessions:
            session_reasons = set(session.reasons)
            if "turn:active" in session_reasons:
                return True
            if (
                "delivery:claimed" in session_reasons
                and "turn:starting" not in session_reasons
            ):
                return True
        reasons = self.reasons + tuple(
            reason for session in self.sessions for reason in session.reasons
        )
        return any(
            reason.startswith("run:") and reason.endswith(":active")
            for reason in reasons
        )

    @property
    def has_runnable_deliveries(self) -> bool:
        return any(
            session.queue_hold_state == "open"
            and any(reason == "delivery:queued" for reason in session.reasons)
            for session in self.sessions
        )

    @property
    def has_runnable_fallback_runs(self) -> bool:
        return any(
            reason.endswith(":runnable") and reason.startswith("run:")
            for reason in self.reasons
        ) or any(
            any(
                reason.endswith(":runnable") and reason.startswith("run:")
                for reason in session.reasons
            )
            for session in self.sessions
        )

    @property
    def needs_session_delivery_wake(self) -> bool:
        return any(
            any(
                reason in {"turn:waiting", "turn:starting"}
                or (
                    reason.startswith("delivery:")
                    and (
                        policy_for(
                            reason.removeprefix("delivery:")
                        ).ordering
                        == "fence"
                        or (
                            session.queue_hold_state == "open"
                            and policy_for(
                                reason.removeprefix("delivery:")
                            ).ordering
                            == "claimable"
                        )
                    )
                )
                for reason in session.reasons
            )
            for session in self.sessions
        )

    @property
    def needs_request_wake(self) -> bool:
        return any(
            reason.startswith("run:")
            and reason.rsplit(":", 1)[-1]
            in {
                SessionRuntimeDisposition.RUNNABLE.value,
                SessionRuntimeDisposition.TRANSITIONING.value,
            }
            for reason in self.reasons
        ) or any(
            any(
                reason.startswith("run:")
                and reason.rsplit(":", 1)[-1]
                in {
                    SessionRuntimeDisposition.RUNNABLE.value,
                    SessionRuntimeDisposition.TRANSITIONING.value,
                }
                for reason in session.reasons
            )
            for session in self.sessions
        )


def _safest(
    values: list[SessionRuntimeDisposition],
) -> SessionRuntimeDisposition:
    if not values:
        return SessionRuntimeDisposition.RECLAIMABLE
    return max(values, key=_DISPOSITION_PRIORITY.__getitem__)


def _activity_payload(row: dict[str, object]) -> dict[str, object] | None:
    try:
        payload = json.loads(str(row.get("payload_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class RuntimeOwnershipProvider:
    """Derive one runtime target disposition from one real SQLite snapshot."""

    def __init__(
        self,
        engine: Engine,
        *,
        after_first_read: Callable[[], None] | None = None,
    ) -> None:
        self.engine = engine
        self._after_first_read = after_first_read

    def snapshot(self, target: RuntimeResourceTarget) -> RuntimeTargetOwnershipSnapshot:
        return self.snapshot_many((target,))[0]

    def snapshot_many(
        self,
        targets: tuple[RuntimeResourceTarget, ...],
    ) -> tuple[RuntimeTargetOwnershipSnapshot, ...]:
        if not targets:
            return ()
        try:
            return self._snapshot_many(targets)
        except Exception:
            logger.exception(
                "Runtime ownership batch lookup failed closed for backend=%s resources=%s",
                targets[0].backend,
                ",".join(str(target.resource_key) for target in targets),
            )
            return tuple(
                self._provider_failure_snapshot(target) for target in targets
            )

    @staticmethod
    def _provider_failure_snapshot(
        target: RuntimeResourceTarget,
    ) -> RuntimeTargetOwnershipSnapshot:
        return RuntimeTargetOwnershipSnapshot(
            backend=target.backend,
            resource_key=target.resource_key,
            activity_runtime_keys=tuple(target.known_activity_runtime_keys),
            sessions=(),
            sessionless_active_activity_ids=(),
            sessionless_fallback_run_ids=(),
            disposition=SessionRuntimeDisposition.UNKNOWN,
            reasons=("provider_failure",),
        )

    def _snapshot_many(
        self,
        targets: tuple[RuntimeResourceTarget, ...],
    ) -> tuple[RuntimeTargetOwnershipSnapshot, ...]:
        normalized_backends = tuple(
            str(target.backend or "").strip() for target in targets
        )
        if any(not backend for backend in normalized_backends):
            raise ValueError("runtime target requires backend and resource_key")
        if len(set(normalized_backends)) != 1:
            raise ValueError("runtime ownership batch requires one backend")
        if any(not str(target.resource_key or "").strip() for target in targets):
            raise ValueError("runtime target requires backend and resource_key")
        backend = normalized_backends[0]

        binding_ids_by_target: list[tuple[str, ...]] = []
        all_binding_ids: set[str] = set()
        for target in targets:
            binding_ids = tuple(
                str(binding.session_id or "").strip()
                for binding in target.bindings
            )
            if any(not session_id for session_id in binding_ids):
                raise ValueError("runtime binding requires an exact session_id")
            if len(set(binding_ids)) != len(binding_ids):
                raise ValueError("runtime bindings require unique session_ids")
            binding_ids_by_target.append(binding_ids)
            all_binding_ids.update(binding_ids)

        connection = self.engine.connect()
        try:
            # Pysqlite defers BEGIN for read-only SQLAlchemy contexts. Emitting it
            # explicitly is the ownership boundary: every following SELECT sees
            # the same WAL generation until this connection is rolled back.
            connection.exec_driver_sql("BEGIN")
            session_filters = []
            if any(target.include_all_backend_sessions for target in targets):
                session_filters.append(agent_sessions.c.agent_backend == backend)
            if all_binding_ids:
                session_filters.append(agent_sessions.c.id.in_(all_binding_ids))
            durable_workdirs = {
                target.durable_session_workdir
                for target in targets
                if target.durable_session_workdir is not None
            }
            if durable_workdirs and not any(
                target.include_all_backend_sessions for target in targets
            ):
                session_filters.append(
                    and_(
                        agent_sessions.c.agent_backend == backend,
                        agent_sessions.c.workdir.in_(durable_workdirs),
                    )
                )
            all_session_rows = (
                [
                    dict(row)
                    for row in connection.execute(
                        select(agent_sessions)
                        .where(or_(*session_filters))
                        .order_by(agent_sessions.c.id)
                    ).mappings()
                ]
                if session_filters
                else []
            )
            all_session_by_id = {
                str(row["id"]): row for row in all_session_rows
            }
            session_rows_by_target: list[list[dict[str, object]]] = []
            for target, binding_ids in zip(
                targets,
                binding_ids_by_target,
                strict=True,
            ):
                for binding in target.bindings:
                    row = all_session_by_id.get(binding.session_id)
                    if row is not None and row.get("workdir") != binding.workdir:
                        raise ValueError(
                            "runtime binding workdir does not match target"
                        )
                if target.include_all_backend_sessions:
                    target_session_rows = [
                        row
                        for row in all_session_rows
                        if str(row.get("agent_backend") or "") == backend
                    ]
                elif target.durable_session_workdir is not None:
                    target_session_rows = [
                        row
                        for row in all_session_rows
                        if str(row.get("agent_backend") or "") == backend
                        and row.get("workdir") == target.durable_session_workdir
                    ]
                else:
                    target_session_rows = [
                        all_session_by_id[session_id]
                        for session_id in binding_ids
                        if session_id in all_session_by_id
                        and str(
                            all_session_by_id[session_id].get("agent_backend") or ""
                        )
                        == backend
                    ]
                session_rows_by_target.append(target_session_rows)

            if self._after_first_read is not None:
                self._after_first_read()

            session_ids = tuple(
                sorted(
                    {
                        str(row["id"])
                        for rows in session_rows_by_target
                        for row in rows
                    }
                )
            )
            delivery_rows = (
                [
                    dict(row)
                    for row in connection.execute(
                        select(message_deliveries)
                        .where(message_deliveries.c.session_id.in_(session_ids))
                        .where(
                            or_(
                                message_deliveries.c.state.is_(None),
                                message_deliveries.c.state.notin_(
                                    _TERMINAL_DELIVERY_STATES
                                ),
                            )
                        )
                        .order_by(
                            message_deliveries.c.session_id,
                            message_deliveries.c.submitted_at,
                            message_deliveries.c.id,
                        )
                    ).mappings()
                ]
                if session_ids
                else []
            )
            turn_rows = (
                [
                    dict(row)
                    for row in connection.execute(
                        select(session_turns)
                        .where(session_turns.c.session_id.in_(session_ids))
                        .where(
                            or_(
                                session_turns.c.state.is_(None),
                                session_turns.c.state != "terminal",
                            )
                        )
                        .order_by(
                            session_turns.c.session_id,
                            session_turns.c.created_at,
                            session_turns.c.id,
                        )
                    ).mappings()
                ]
                if session_ids
                else []
            )
            activity_rows = [
                dict(row)
                for row in connection.execute(
                    select(runtime_records)
                    .where(runtime_records.c.record_type == ACTIVITY_RECORD_TYPE)
                    .where(
                        func.json_extract(
                            runtime_records.c.payload_json,
                            "$.activity.backend",
                        )
                        == backend
                    )
                    .order_by(runtime_records.c.created_at, runtime_records.c.id)
                ).mappings()
            ]
            resolved_agent = agents.alias("resolved_agent")
            resolved_run_session = agent_sessions.alias("resolved_run_session")
            missing_run_backend = or_(
                agent_runs.c.agent_backend.is_(None),
                func.trim(agent_runs.c.agent_backend) == "",
            )
            run_rows = [
                dict(row)
                for row in connection.execute(
                    select(
                        agent_runs,
                        resolved_agent.c.backend.label("resolved_agent_backend"),
                        resolved_run_session.c.agent_backend.label(
                            "resolved_session_backend"
                        ),
                    )
                    .select_from(
                        agent_runs.outerjoin(
                            resolved_agent,
                            or_(
                                agent_runs.c.agent_id == resolved_agent.c.id,
                                and_(
                                    agent_runs.c.agent_id.is_(None),
                                    agent_runs.c.agent_name
                                    == resolved_agent.c.name,
                                ),
                            ),
                        ).outerjoin(
                            resolved_run_session,
                            agent_runs.c.session_id == resolved_run_session.c.id,
                        )
                    )
                    .where(agent_runs.c.status.notin_(_TERMINAL_RAW_RUN_STATUSES))
                    .where(
                        or_(
                            agent_runs.c.run_type.is_(None),
                            agent_runs.c.run_type != "watch_runtime",
                        )
                    )
                    .where(
                        or_(
                            agent_runs.c.agent_backend == backend,
                            and_(
                                missing_run_backend,
                                or_(
                                    resolved_agent.c.backend == backend,
                                    resolved_run_session.c.agent_backend == backend,
                                    agent_runs.c.agent_name == backend,
                                ),
                            ),
                        )
                    )
                    .order_by(agent_runs.c.created_at, agent_runs.c.id)
                ).mappings()
            ]
            represented_ids = tuple(
                sorted(
                    {
                        str(row["delivery_id"])
                        for row in run_rows
                        if row.get("delivery_id")
                    }
                )
            )
            represented_delivery_ids = (
                {
                    str(value)
                    for value in connection.execute(
                        select(message_deliveries.c.id).where(
                            message_deliveries.c.id.in_(represented_ids)
                        )
                    ).scalars()
                }
                if represented_ids
                else set()
            )
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.close()

        snapshots = []
        for target, session_rows in zip(
            targets,
            session_rows_by_target,
            strict=True,
        ):
            target_session_ids = {str(row["id"]) for row in session_rows}
            snapshots.append(
                self._derive(
                    target,
                    session_rows=session_rows,
                    delivery_rows=[
                        row
                        for row in delivery_rows
                        if str(row["session_id"]) in target_session_ids
                    ],
                    turn_rows=[
                        row
                        for row in turn_rows
                        if str(row["session_id"]) in target_session_ids
                    ],
                    activity_rows=activity_rows,
                    run_rows=run_rows,
                    represented_delivery_ids=represented_delivery_ids,
                )
            )
        return tuple(snapshots)

    @staticmethod
    def _derive(
        target: RuntimeResourceTarget,
        *,
        session_rows: list[dict[str, object]],
        delivery_rows: list[dict[str, object]],
        turn_rows: list[dict[str, object]],
        activity_rows: list[dict[str, object]],
        run_rows: list[dict[str, object]],
        represented_delivery_ids: set[str],
    ) -> RuntimeTargetOwnershipSnapshot:
        session_by_id = {str(row["id"]): row for row in session_rows}
        facts: dict[str, list[SessionRuntimeDisposition]] = {
            session_id: [] for session_id in session_by_id
        }
        reasons: dict[str, list[str]] = {session_id: [] for session_id in session_by_id}
        delivery_ids: dict[str, list[str]] = {
            session_id: [] for session_id in session_by_id
        }
        turn_ids: dict[str, list[str]] = {
            session_id: [] for session_id in session_by_id
        }
        activity_ids: dict[str, list[str]] = {
            session_id: [] for session_id in session_by_id
        }
        run_ids: dict[str, list[str]] = {
            session_id: [] for session_id in session_by_id
        }

        waiting_turns = {
            str(row["id"]): row
            for row in turn_rows
            if str(row.get("state") or "") == "waiting"
        }
        paired_waiting_turn_ids: set[str] = set()
        for row in delivery_rows:
            session_id = str(row["session_id"])
            state = str(row["state"])
            delivery_ids[session_id].append(str(row["id"]))
            if state == "interrupt_waiting":
                turn_id = str(row.get("turn_id") or "")
                waiting_turn = waiting_turns.get(turn_id)
                exact_pair = bool(
                    waiting_turn is not None
                    and str(waiting_turn.get("session_id") or "") == session_id
                    and str(waiting_turn.get("initial_delivery_id") or "")
                    == str(row["id"])
                    and str(row.get("turn_role") or "") == "initial"
                    and row.get("turn_position") == 0
                )
                disposition = (
                    SessionRuntimeDisposition.TRANSITIONING
                    if exact_pair
                    else SessionRuntimeDisposition.UNKNOWN
                )
                if exact_pair:
                    paired_waiting_turn_ids.add(turn_id)
                facts[session_id].append(disposition)
                reasons[session_id].append(f"delivery:{state}")
                continue
            try:
                ordering = policy_for(state).ordering
            except ValueError:
                disposition = SessionRuntimeDisposition.UNKNOWN
            else:
                if ordering == "turn_owned":
                    disposition = SessionRuntimeDisposition.ACTIVE
                elif ordering == "fence":
                    disposition = SessionRuntimeDisposition.TRANSITIONING
                elif ordering == "claimable":
                    held = str(session_by_id[session_id].get("queue_hold_state") or "open")
                    disposition = (
                        SessionRuntimeDisposition.WAITING
                        if held == "held"
                        else SessionRuntimeDisposition.RUNNABLE
                    )
                else:
                    disposition = SessionRuntimeDisposition.RECLAIMABLE
            facts[session_id].append(disposition)
            reasons[session_id].append(f"delivery:{state}")

        for row in turn_rows:
            session_id = str(row["session_id"])
            state = str(row["state"])
            turn_id = str(row["id"])
            turn_ids[session_id].append(turn_id)
            if state in {"starting", "active"}:
                disposition = SessionRuntimeDisposition.ACTIVE
            elif state == "waiting":
                disposition = (
                    SessionRuntimeDisposition.TRANSITIONING
                    if turn_id in paired_waiting_turn_ids
                    else SessionRuntimeDisposition.UNKNOWN
                )
            else:
                disposition = SessionRuntimeDisposition.UNKNOWN
            facts[session_id].append(disposition)
            reasons[session_id].append(f"turn:{state}")

        target_activity_keys = {
            key
            for binding in target.bindings
            for key in binding.activity_runtime_keys
        }
        if target.maps_all_backend_activities:
            target_activity_keys.update(target.known_activity_runtime_keys)
        known_activity_keys = set(target.known_activity_runtime_keys) | target_activity_keys
        sessionless_activity_ids: list[str] = []
        target_facts: list[SessionRuntimeDisposition] = []
        target_reasons: list[str] = []
        for row in activity_rows:
            payload = _activity_payload(row)
            if payload is None:
                target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                target_reasons.append("activity:invalid_payload")
                continue
            phase = str(payload.get("phase") or "")
            activity = payload.get("activity")
            if not isinstance(activity, dict):
                target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                target_reasons.append("activity:invalid_shape")
                continue
            runtime_key = str(activity.get("runtime_key") or "")
            activity_id = str(activity.get("id") or row["id"])
            session_id = str(activity.get("session_id") or "")
            if phase not in _KNOWN_ACTIVITY_PHASES:
                if (
                    not target.maps_all_backend_activities
                    and runtime_key not in known_activity_keys
                    and session_id not in facts
                ):
                    continue
                target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                target_reasons.append(f"activity:{activity_id}:unknown_phase")
                continue
            if phase != ACTIVE_PHASE:
                continue
            if not target.maps_all_backend_activities and runtime_key not in known_activity_keys:
                target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                target_reasons.append(f"activity:{activity_id}:unmapped")
                continue
            if (
                runtime_key not in target_activity_keys
                and not target.maps_all_backend_activities
                and session_id not in facts
            ):
                continue
            if session_id:
                if session_id not in facts:
                    target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                    target_reasons.append(f"activity:{activity_id}:missing_session")
                    continue
                activity_ids[session_id].append(activity_id)
                facts[session_id].append(SessionRuntimeDisposition.ACTIVE)
                reasons[session_id].append(f"activity:{activity_id}:active")
            else:
                sessionless_activity_ids.append(activity_id)
                target_facts.append(SessionRuntimeDisposition.ACTIVE)
                target_reasons.append(f"activity:{activity_id}:sessionless_active")

        route_to_session: dict[str, str] = {}
        ambiguous_route_keys: set[str] = set()
        for binding in target.bindings:
            matched_session = (
                binding.session_id if binding.session_id in session_by_id else None
            )
            for route_key in binding.fallback_route_keys:
                existing = route_to_session.get(route_key)
                if (
                    matched_session is not None
                    and existing is not None
                    and existing != matched_session
                ):
                    route_to_session.pop(route_key, None)
                    ambiguous_route_keys.add(route_key)
                elif matched_session is not None and route_key not in ambiguous_route_keys:
                    route_to_session[route_key] = matched_session
        target_route_keys = {
            route_key
            for binding in target.bindings
            for route_key in binding.fallback_route_keys
        }
        known_route_keys = set(target.known_fallback_route_keys) | target_route_keys
        sessionless_run_ids: list[str] = []
        for row in run_rows:
            represented = str(row.get("delivery_id") or "")
            if represented and represented in represented_delivery_ids:
                continue
            status = normalize_run_status(row.get("status"))
            run_type = str(row.get("run_type") or "")
            if run_type not in EXECUTION_RUN_TYPES:
                disposition = SessionRuntimeDisposition.UNKNOWN
            elif status == "queued":
                disposition = SessionRuntimeDisposition.RUNNABLE
            elif status == "running":
                disposition = (
                    SessionRuntimeDisposition.ACTIVE
                    if row.get("pid") is not None
                    else SessionRuntimeDisposition.TRANSITIONING
                )
            else:
                disposition = SessionRuntimeDisposition.UNKNOWN
            run_id = str(row["id"])
            session_id = str(row.get("session_id") or "")
            if session_id:
                if session_id not in facts:
                    route_key = str(row.get("legacy_session_key") or "")
                    current_session_backend = str(
                        row.get("resolved_session_backend") or ""
                    ).strip()
                    if current_session_backend == target.backend:
                        continue
                    if target.maps_all_backend_fallback_runs:
                        sessionless_run_ids.append(run_id)
                        target_facts.append(disposition)
                        target_reasons.append(
                            f"run:{run_id}:detached_session:{disposition.value}"
                        )
                    elif route_key in target_route_keys:
                        sessionless_run_ids.append(run_id)
                        target_facts.append(disposition)
                        target_reasons.append(
                            f"run:{run_id}:detached_session:{disposition.value}"
                        )
                    elif route_key not in known_route_keys:
                        target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                        target_reasons.append(f"run:{run_id}:unmapped_route")
                    continue
                run_ids[session_id].append(run_id)
                facts[session_id].append(disposition)
                reasons[session_id].append(f"run:{run_id}:{disposition.value}")
                continue
            route_key = str(row.get("legacy_session_key") or "")
            if target.maps_all_backend_fallback_runs:
                sessionless_run_ids.append(run_id)
                target_facts.append(disposition)
                target_reasons.append(
                    f"run:{run_id}:sessionless:{disposition.value}"
                )
                continue
            if route_key in target_route_keys:
                if route_key in ambiguous_route_keys:
                    target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                    target_reasons.append(f"run:{run_id}:ambiguous_route")
                    continue
                mapped_session = route_to_session.get(route_key)
                sessionless_run_ids.append(run_id)
                if mapped_session is not None:
                    run_ids[mapped_session].append(run_id)
                    facts[mapped_session].append(disposition)
                    reasons[mapped_session].append(
                        f"run:{run_id}:sessionless:{disposition.value}"
                    )
                else:
                    target_facts.append(disposition)
                    target_reasons.append(
                        f"run:{run_id}:sessionless:{disposition.value}"
                    )
            elif route_key in known_route_keys:
                continue
            else:
                target_facts.append(SessionRuntimeDisposition.UNKNOWN)
                target_reasons.append(f"run:{run_id}:unmapped_route")

        snapshots: list[SessionRuntimeOwnershipSnapshot] = []
        for session_id, row in session_by_id.items():
            disposition = _safest(facts[session_id])
            snapshots.append(
                SessionRuntimeOwnershipSnapshot(
                    session_id=session_id,
                    disposition=disposition,
                    delivery_ids=tuple(delivery_ids[session_id]),
                    turn_ids=tuple(turn_ids[session_id]),
                    active_activity_ids=tuple(activity_ids[session_id]),
                    fallback_run_ids=tuple(run_ids[session_id]),
                    queue_hold_state=str(row.get("queue_hold_state") or "open"),
                    reasons=tuple(reasons[session_id]),
                )
            )
        target_disposition = _safest(
            target_facts + [snapshot.disposition for snapshot in snapshots]
        )
        return RuntimeTargetOwnershipSnapshot(
            backend=target.backend,
            resource_key=target.resource_key,
            activity_runtime_keys=tuple(sorted(target_activity_keys)),
            sessions=tuple(snapshots),
            sessionless_active_activity_ids=tuple(sessionless_activity_ids),
            sessionless_fallback_run_ids=tuple(sessionless_run_ids),
            disposition=target_disposition,
            reasons=tuple(target_reasons),
        )


def wake_runtime_ownership(controller: object, snapshot: RuntimeTargetOwnershipSnapshot) -> None:
    """Emit only coalescing recovery hints implied by an ownership snapshot."""

    supervisor = getattr(controller, "runtime_work_supervisor", None)
    notify = getattr(supervisor, "notify", None)
    if not callable(notify):
        return
    from core.runtime_work import RuntimeWorkLane

    lanes: list[RuntimeWorkLane] = []
    if snapshot.needs_session_delivery_wake:
        lanes.append(RuntimeWorkLane.SESSION_DELIVERIES)
    if snapshot.needs_request_wake:
        lanes.append(RuntimeWorkLane.REQUESTS)
    if lanes:
        notify(*lanes)
