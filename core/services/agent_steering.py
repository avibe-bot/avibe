"""Backend-native insertion into the currently active Avibe Turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from modules.agents.base import AgentRequest

AGENT_TURN_TOKEN = "turn_token"
AGENT_RUNTIME_TURN_TOKEN = "agent_runtime_turn_token"


class SteerOutcome(str, Enum):
    """Exhaustive result of one backend-native steering attempt."""

    ACCEPTED = "accepted"
    NOT_ACTIVE = "not_active"
    REFUSED = "refused"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SteerRequest:
    """One guarded insertion request carrying the original user text."""

    target_session_id: str
    expected_logical_turn_id: str
    expected_native_turn_id: str
    text: str


@dataclass(frozen=True)
class SteerResult:
    """Typed receipt; backend diagnostics remain subordinate to ``outcome``."""

    outcome: SteerOutcome
    reason: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveSteerTarget:
    """Live AgentService gate state passed to a backend adapter."""

    runtime_key: str
    logical_turn_id: str
    context: Any
    agent_request: AgentRequest | None
    agent: Any


def result(
    outcome: SteerOutcome,
    *,
    reason: str | None = None,
    **details: Any,
) -> SteerResult:
    return SteerResult(outcome=outcome, reason=reason, details=details)


def _context_session_ids(context: Any) -> set[str]:
    payload = getattr(context, "platform_specific", None) or {}
    values: set[str] = set()
    target = payload.get("agent_session_target") if isinstance(payload, dict) else None
    if isinstance(target, dict):
        values.add(str(target.get("id") or "").strip())
    if isinstance(payload, dict):
        values.add(str(payload.get("agent_session_id") or "").strip())
    values.discard("")
    return values


def _active_target(controller: Any, backend: str, session_id: str) -> ActiveSteerTarget | None:
    service = getattr(controller, "agent_service", None)
    gates = getattr(service, "_turn_gates", None)
    if not isinstance(gates, dict):
        return None

    matches: list[ActiveSteerTarget] = []
    for runtime_key, gate in list(gates.items()):
        if str(getattr(gate, "backend", "") or "") != backend:
            continue
        if not str(getattr(gate, "token", "") or "") or not bool(
            getattr(gate, "runtime_started", False)
        ):
            continue
        task = getattr(gate, "task", None)
        if task is None or task.done():
            continue
        agent_request = getattr(gate, "request", None)
        context = getattr(gate, "context", None) or getattr(agent_request, "context", None)
        if context is None or session_id not in _context_session_ids(context):
            continue
        payload = getattr(context, "platform_specific", None) or {}
        runtime_turn_id = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if runtime_turn_id != str(getattr(gate, "token", "") or ""):
            continue
        logical_turn_id = str(payload.get(AGENT_TURN_TOKEN) or "").strip()
        if not logical_turn_id:
            continue
        agent = getattr(gate, "agent", None)
        if agent is None:
            continue
        matches.append(
            ActiveSteerTarget(
                runtime_key=str(runtime_key),
                logical_turn_id=logical_turn_id,
                context=context,
                agent_request=agent_request,
                agent=agent,
            )
        )

    return matches[0] if len(matches) == 1 else None


def active_steer_identity(controller: Any, backend: str, session_id: str) -> tuple[str, str] | None:
    """Return the current logical/native identity pair for a steerable Turn."""

    target = _active_target(controller, backend, session_id)
    if target is None:
        return None
    native_identity = getattr(target.agent, "steering_native_turn_id", None)
    if not callable(native_identity):
        return None
    native_turn_id = str(native_identity(target) or "").strip()
    if not native_turn_id:
        return None
    return target.logical_turn_id, native_turn_id


async def steer_active_turn(
    controller: Any,
    backend: str,
    request: SteerRequest,
) -> SteerResult:
    """Insert text through a registered backend without entering normal dispatch."""

    target = _active_target(controller, backend, request.target_session_id)
    if target is None:
        service = getattr(controller, "agent_service", None)
        if getattr(service, "agents", {}).get(backend) is None:
            return result(
                SteerOutcome.REFUSED,
                reason="runtime_unavailable",
                backend=backend,
            )
        return result(SteerOutcome.NOT_ACTIVE, reason="no_matching_active_turn", backend=backend)
    if target.logical_turn_id != request.expected_logical_turn_id:
        return result(SteerOutcome.NOT_ACTIVE, reason="stale_logical_turn", backend=backend)

    steer = getattr(target.agent, "steer_active_turn", None)
    if not callable(steer):
        return result(SteerOutcome.REFUSED, reason="unsupported", backend=backend)
    receipt = await steer(request, target)
    if not isinstance(receipt, SteerResult):
        return result(SteerOutcome.UNKNOWN, reason="invalid_adapter_receipt", backend=backend)
    return receipt
