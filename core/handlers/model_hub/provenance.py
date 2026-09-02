"""Exact-attribution turn provenance for process-scoped Model Hub traffic."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from config.v2_config import ModelHubConfig

from core.run_settlement import (
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from vibe.i18n import t as i18n_t

from .adapter import RawCallOutcome
from .classification import ResolutionDecision, ResolutionReason
from .events import (
    EVENT_REASON_AUTHORITY,
    RETIRED_PERSISTED_REASON_DEGRADATIONS,
    SOURCE_DETAIL_EVENT_REASONS,
    event_reason_label,
)
from .resolver import ModelHubTurnResolution, source_eligible_for_backend
from .state_file import write_state_document


BackendName = Literal["claude", "codex", "opencode"]
SupplyChannel = Literal["native_cli", "hub"]
SupplyState = Literal["waiting", "interrupted"]
ScopeKey = tuple[BackendName, str]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptIdentity:
    source_id: str
    resolved_model_id: str
    channel: SupplyChannel

    def payload(self) -> dict:
        return {
            "source_id": self.source_id,
            "configured_model_id": self.resolved_model_id,
            "channel": self.channel,
        }


@dataclass(frozen=True)
class ExactHopBlocker:
    source_id: str
    model_id: str
    reason: str

    def payload(self) -> dict:
        return {
            "source_id": self.source_id,
            "model_id": self.model_id,
            "reason": self.reason,
        }


def exact_hop_blockers(
    resolution: ModelHubTurnResolution,
) -> tuple[ExactHopBlocker, ...]:
    """Project every blocked persisted hop from the canonical live inspection."""

    blockers = []
    for inspection in resolution.inspected_hops:
        if (
            inspection.runnable
            or inspection.source_id is None
            or inspection.model_id is None
        ):
            continue
        reason = inspection.reason
        if (
            inspection.source is not None
            and EVENT_REASON_AUTHORITY.get(str(reason)) != "structural"
        ):
            if inspection.source.state.status == "cooldown":
                reason = "cooldown"
            elif inspection.source.state.detail_key is not None:
                reason = SOURCE_DETAIL_EVENT_REASONS.get(
                    inspection.source.state.detail_key,
                    reason,
                )
        if reason is None:
            continue
        blockers.append(
            ExactHopBlocker(
                source_id=inspection.source_id,
                model_id=inspection.model_id,
                reason=reason,
            )
        )
    return tuple(blockers)


@dataclass(frozen=True)
class TurnSupplyBlocker:
    source: str
    reason: str


@dataclass(frozen=True)
class TurnSupplyFacts:
    backend: BackendName
    model: str
    supply_state: SupplyState
    source: str = ""
    retry_at: str = ""
    blockers: tuple[TurnSupplyBlocker, ...] = ()


@dataclass(frozen=True)
class TurnOutcomeRenderingRule:
    outcome: str
    discriminator: str
    copy_keys: tuple[tuple[str, str | None], ...]


# This is the only executable projection of the authoritative section 4.5 matrix.
TURN_OUTCOME_RENDERING_AUTHORITY: dict[str, TurnOutcomeRenderingRule] = {
    "turn.served": TurnOutcomeRenderingRule(
        outcome="served",
        discriminator="any",
        copy_keys=(("default", None),),
    ),
    "turn.exhausted": TurnOutcomeRenderingRule(
        outcome="exhausted",
        discriminator="final_supply_state",
        copy_keys=(
            ("waiting", "modelHub.launch.waiting"),
            ("waiting_without_retry", "modelHub.launch.waiting_without_retry"),
            ("interrupted", "modelHub.launch.interrupted"),
        ),
    ),
    "turn.request_nonfallback": TurnOutcomeRenderingRule(
        outcome="failed_terminal",
        discriminator="request_nonfallback",
        copy_keys=(("default", "modelHub.launch.request_incompatible"),),
    ),
    "turn.engine_down": TurnOutcomeRenderingRule(
        outcome="failed_terminal",
        discriminator="engine_down",
        copy_keys=(
            ("default", "modelHub.errors.engine_down"),
            ("stream_started", "modelHub.errors.engine_down_streamed"),
        ),
    ),
    "turn.streamed_fallback": TurnOutcomeRenderingRule(
        outcome="failed_terminal",
        discriminator="streamed_fallback",
        copy_keys=(
            ("next_current", "modelHub.launch.retry"),
            ("waiting", "modelHub.launch.waiting"),
            ("interrupted", "modelHub.launch.interrupted"),
            ("transition_unpersisted", "modelHub.errors.stream_interrupted"),
        ),
    ),
    "turn.no_candidate.unconfigured": TurnOutcomeRenderingRule(
        outcome="no_candidate",
        discriminator="route_unconfigured",
        copy_keys=(("interrupted", "modelHub.launch.route_unconfigured"),),
    ),
    "turn.no_candidate.blocked": TurnOutcomeRenderingRule(
        outcome="no_candidate",
        discriminator="blocked_supply_state",
        copy_keys=(
            ("waiting", "modelHub.launch.waiting"),
            ("waiting_without_retry", "modelHub.launch.waiting_without_retry"),
            ("interrupted", "modelHub.launch.interrupted"),
        ),
    ),
    "turn.canceled": TurnOutcomeRenderingRule(
        outcome="canceled",
        discriminator="fsm_canceled",
        copy_keys=(("default", None),),
    ),
}


@dataclass(frozen=True)
class TurnOutcomeProjectionInput:
    outcome: str
    discriminator: str
    supply_facts: TurnSupplyFacts | None = None
    stream_started: bool = False
    next_current_changed: bool = False
    source_transition_persisted: bool | None = None


class TurnOutcomeProductionError(ValueError):
    """A terminal outcome is missing a fact required by the copy matrix."""


def _turn_outcome_rule(
    projection: TurnOutcomeProjectionInput,
) -> TurnOutcomeRenderingRule:
    matches = tuple(
        rule
        for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
        if rule.outcome == projection.outcome
        and rule.discriminator == projection.discriminator
    )
    if len(matches) != 1:
        raise TurnOutcomeProductionError(
            "Turn outcome does not match its rendering discriminator"
        )
    return matches[0]


def _turn_outcome_variant(
    projection: TurnOutcomeProjectionInput,
    rule: TurnOutcomeRenderingRule,
) -> str:
    copy_keys = dict(rule.copy_keys)
    if (
        projection.source_transition_persisted is False
        and "transition_unpersisted" in copy_keys
    ):
        return "transition_unpersisted"
    if projection.next_current_changed and "next_current" in copy_keys:
        return "next_current"
    if projection.stream_started and "stream_started" in copy_keys:
        return "stream_started"
    if (
        projection.supply_facts is not None
        and projection.supply_facts.supply_state == "waiting"
        and not projection.supply_facts.retry_at
        and "waiting_without_retry" in copy_keys
    ):
        return "waiting_without_retry"
    if (
        projection.supply_facts is not None
        and projection.supply_facts.supply_state in copy_keys
    ):
        return projection.supply_facts.supply_state
    return "default"


def produce_turn_outcome(
    decision: str,
    *,
    config: ModelHubConfig | None = None,
    resolution: ModelHubTurnResolution | None = None,
    attempted_hop: tuple[str, str] | None = None,
    stream_started: bool = False,
    source_transition_persisted: bool | None = None,
) -> TurnOutcomeProjectionInput:
    """Produce complete terminal facts from one authoritative matrix row."""

    rule = TURN_OUTCOME_RENDERING_AUTHORITY.get(decision)
    if rule is None:
        raise TurnOutcomeProductionError("Unknown turn-outcome matrix decision")
    variants = {variant for variant, _key in rule.copy_keys}
    if decision == "turn.streamed_fallback" and source_transition_persisted is None:
        raise TurnOutcomeProductionError(
            "Streamed fallback is missing its Source-transition persistence fact"
        )
    requires_exact_supply = bool(
        variants & {"next_current", "waiting", "interrupted"}
    ) and source_transition_persisted is not False
    if requires_exact_supply and (config is None or resolution is None):
        raise TurnOutcomeProductionError(
            "Turn outcome production is missing its exact-chain inspection"
        )

    next_current_changed = False
    supply_facts = None
    if requires_exact_supply:
        assert config is not None and resolution is not None
        if "next_current" in variants:
            if attempted_hop is None:
                raise TurnOutcomeProductionError(
                    "Turn outcome production is missing its attempted hop"
                )
            next_hop = (
                resolution.candidate_hops[0]
                if resolution.candidate_hops
                else None
            )
            if next_hop is not None:
                next_identity = (next_hop.source_id, next_hop.model_id)
                if next_identity == attempted_hop:
                    raise TurnOutcomeProductionError(
                        "Settled streamed fallback left the attempted hop current"
                    )
                next_current_changed = True
        if not next_current_changed:
            recovered_after_exhaustion = (
                decision in {"turn.exhausted", "turn.no_candidate.blocked"}
                and resolution.supply_status in {"ok", "degraded"}
                and bool(resolution.candidate_hops)
            )
            if recovered_after_exhaustion:
                supply_facts = TurnSupplyFacts(
                    backend=resolution.backend,
                    model=resolution.requested_model or resolution.target_model,
                    supply_state="waiting",
                )
            elif resolution.supply_status not in {"waiting", "interrupted"}:
                raise TurnOutcomeProductionError(
                    "Turn outcome production requires a terminal supply state"
                )
            else:
                supply_facts = turn_supply_facts(config, resolution)

    projection = TurnOutcomeProjectionInput(
        outcome=rule.outcome,
        discriminator=rule.discriminator,
        supply_facts=supply_facts,
        stream_started=stream_started,
        next_current_changed=next_current_changed,
        source_transition_persisted=source_transition_persisted,
    )
    if _turn_outcome_variant(projection, rule) not in dict(rule.copy_keys):
        raise TurnOutcomeProductionError(
            "Turn outcome production is missing its required rendering fact"
        )
    return projection


REQUEST_NONFALLBACK_TURN_OUTCOME = produce_turn_outcome(
    "turn.request_nonfallback"
)
ENGINE_DOWN_TURN_OUTCOME = produce_turn_outcome("turn.engine_down")


@dataclass(frozen=True)
class TurnOutcomeCopy:
    key: str
    params: Mapping[str, Any]


def supply_interruption_reason(
    config: ModelHubConfig,
    resolution: ModelHubTurnResolution,
) -> str:
    """Return the exact-chain structural reason used by events and copy facts."""

    structural_reason = resolution.structural_blocker_reason
    if structural_reason in {
        "route_unconfigured",
        "source_missing",
        "model_unsupported",
        "native_cli_unavailable",
    }:
        return structural_reason
    order = config.effective_source_order(resolution.backend)
    sources_by_id = {source.id: source for source in config.sources}
    enabled_sources = [
        sources_by_id[source_id]
        for source_id in order
        if source_id in sources_by_id
    ]
    if not enabled_sources:
        if config.sources and not any(
            source_eligible_for_backend(source, resolution.backend)
            for source in config.sources
        ):
            return "no_eligible_source"
        return "no_enabled_source"
    if not any(
        source_eligible_for_backend(source, resolution.backend)
        for source in enabled_sources
    ):
        return "no_eligible_source"
    return "model_unsupported"


def turn_supply_facts(
    config: ModelHubConfig,
    resolution: ModelHubTurnResolution,
) -> TurnSupplyFacts:
    """Project user-visible facts from one canonical exact-chain inspection."""

    model = resolution.requested_model or resolution.target_model
    supply_state: SupplyState = (
        "waiting" if resolution.supply_status == "waiting" else "interrupted"
    )
    cooling = tuple(
        source
        for source in resolution.matching_sources
        if source.state.status == "cooldown" and source.state.retry_at
    )
    blockers: list[TurnSupplyBlocker] = []
    for inspection in resolution.inspected_hops:
        if inspection.runnable:
            continue
        source = inspection.source
        reason = inspection.reason
        if reason not in EVENT_REASON_AUTHORITY and source is not None:
            reason = SOURCE_DETAIL_EVENT_REASONS.get(
                source.state.detail_key or "",
                reason,
            )
        if reason not in EVENT_REASON_AUTHORITY:
            continue
        blockers.append(
            TurnSupplyBlocker(
                source=(
                    source.display_name
                    if source is not None
                    else str(inspection.source_id or "")
                ),
                reason=reason,
            )
        )
    if supply_state == "interrupted" and not blockers:
        blockers.append(
            TurnSupplyBlocker(
                source="",
                reason=supply_interruption_reason(config, resolution),
            )
        )
    return TurnSupplyFacts(
        backend=resolution.backend,
        model=model,
        supply_state=supply_state,
        source=", ".join(source.display_name for source in cooling),
        retry_at=min(
            (source.state.retry_at or "" for source in cooling),
            default="",
        ),
        blockers=tuple(blockers),
    )


def project_turn_outcome_copy(
    projection: TurnOutcomeProjectionInput,
) -> TurnOutcomeCopy | None:
    """Project copy from the recorded outcome and its sole matrix discriminator."""

    rule = _turn_outcome_rule(projection)
    copy_keys = dict(rule.copy_keys)
    variant = _turn_outcome_variant(projection, rule)
    if variant not in copy_keys:
        raise TurnOutcomeProductionError(
            "Turn outcome bypassed production without its required rendering fact"
        )
    key = copy_keys[variant]
    if key is None:
        return None
    facts = projection.supply_facts
    return TurnOutcomeCopy(
        key=key,
        params={
            "model": facts.model if facts is not None else "",
            "backend": facts.backend if facts is not None else "",
            "source": facts.source if facts is not None else "",
            "retry_at": facts.retry_at if facts is not None else "",
            "blockers": facts.blockers if facts is not None else (),
        },
    )


def render_turn_outcome_copy(
    projection: TurnOutcomeProjectionInput,
    language: str,
) -> str | None:
    copy = project_turn_outcome_copy(projection)
    if copy is None:
        return None
    params = dict(copy.params)
    blockers = params.get("blockers", ())
    if isinstance(blockers, tuple):
        rendered = []
        for blocker in blockers:
            if not isinstance(blocker, TurnSupplyBlocker):
                continue
            label = event_reason_label(blocker.reason, language)
            rendered.append(
                f"{blocker.source}: {label}" if blocker.source else label
            )
        params["blockers"] = ", ".join(rendered)
    return i18n_t(copy.key, language, **params)


@dataclass
class TurnTrace:
    turn_id: str
    agent: BackendName
    requested_model_id: str
    scope_key: ScopeKey
    failed_attempts: list[dict] = field(default_factory=list)
    served: Optional[dict] = None
    terminal_error: Optional[dict] = None
    pending_attempt: Optional[AttemptIdentity] = None
    model_supply_state: Optional[SupplyState] = None
    blockers: list[dict] = field(default_factory=list)
    gateway_source_id: Optional[str] = None
    gateway_model_id: Optional[str] = None
    ambiguous: bool = False
    terminal_outcome: TurnOutcomeProjectionInput | None = None


@dataclass
class ProcessScope:
    token: str
    active_turns: set[str] = field(default_factory=set)
    ambiguous_turns: set[str] = field(default_factory=set)
    prepared_routes: dict[str, "PreparedGatewayRoute"] = field(default_factory=dict)
    routing_conflicts: set[str] = field(default_factory=set)
    untracked_use: bool = False
    # Every caller model this scope has routed each gateway model from, so the
    # process stays routable between turns. `prepared_routes` is pruned at
    # settlement because it answers "who does this request belong to"; this
    # answers "can this process reach this model at all", which outlives any
    # one turn window. Callers accumulate and are never overwritten: a route
    # that was prepared but never activated can then only ever widen the set,
    # so the worst it can do is make the model ambiguous and fail closed,
    # never re-point the running process at someone else's route.
    route_callers: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedGatewayRoute:
    requested_model_id: str
    resolved_model_id: str
    source_id: str


@dataclass(frozen=True)
class GatewayRouting:
    """Where a gateway request goes, and which turn — if any — owns it."""

    caller_model_id: Optional[str]
    owner_turn_id: Optional[str]


class GatewayTurnTerminalizer:
    """One funnel for every exit after an exact gateway identity is prepared."""

    def __init__(
        self,
        registry: "TurnCorrelationRegistry",
        *,
        backend: str,
        token: str,
    ) -> None:
        self._registry = registry
        self._backend = backend
        self._token = token
        opened = registry._open_prepared_gateway_turn(
            backend=backend,
            token=token,
        )
        # Keep the exact identity object armed for *this* request, not just its
        # value: it is the only thing that distinguishes an untouched
        # preparation from the turn's single pending slot having since been
        # written by someone else. See `clear_prepared_attempt`.
        self.turn_id, self._prepared_attempt = opened or (None, None)
        self._stream_started = False
        self._attempt_started = False
        self._downstream_canceled = False

    def __enter__(self) -> "GatewayTurnTerminalizer":
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        if self._downstream_canceled or exc_type is asyncio.CancelledError:
            return
        self._registry._terminalize_gateway_exit(
            self.turn_id,
            stream_started=self._stream_started,
        )

    def resolution_model(self, gateway_model_id: str) -> Optional[str]:
        """Return the uniquely prepared caller model for this gateway request."""

        routing = self._registry.claim_gateway_request(
            backend=self._backend,
            token=self._token,
            prepared_turn_id=self.turn_id,
            prepared_attempt=self._prepared_attempt,
            gateway_model_id=gateway_model_id,
        )
        self.turn_id = routing.owner_turn_id
        return routing.caller_model_id

    def fail(
        self,
        reason: Literal["invalid_parameter", "protocol_error"],
    ) -> None:
        self._registry._terminalize_gateway_exit(
            self.turn_id,
            reason=reason,
            stream_started=self._stream_started,
            force=True,
        )

    def engine_down(self) -> None:
        self._registry._terminalize_gateway_exit(
            self.turn_id,
            reason="engine_down",
            stream_started=self._stream_started,
            force=True,
        )

    def mark_no_candidate(
        self,
        supply_state: SupplyState,
        blockers: Iterable[ExactHopBlocker] = (),
    ) -> None:
        self._registry.mark_gateway_no_candidate(
            self.turn_id,
            supply_state,
            blockers,
        )

    def begin_attempt(
        self,
        *,
        source_id: str,
        resolved_model_id: str,
        channel: SupplyChannel,
        via_mapping: bool,
    ) -> None:
        self._attempt_started = True
        self._registry.begin_attempt(
            self.turn_id,
            source_id=source_id,
            resolved_model_id=resolved_model_id,
            channel=channel,
            via_mapping=via_mapping,
        )

    def finish_attempt(
        self,
        *,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
    ) -> None:
        self._attempt_started = True
        self._registry.finish_attempt(
            self.turn_id,
            outcome=outcome,
            decision=decision,
        )

    def mark_stream_started(self) -> None:
        self._stream_started = True

    def record_turn_outcome(
        self,
        turn_outcome: TurnOutcomeProjectionInput | None,
    ) -> None:
        """Keep the settlement projection attached to the correlated turn event."""

        if self.turn_id is not None:
            self._registry.record_turn_outcome(self.turn_id, turn_outcome)

    def mark_downstream_canceled(self) -> None:
        """Clear a prepared-only attempt before the outer stopped settlement."""

        if not self._attempt_started:
            self._registry.clear_prepared_attempt(self.turn_id)
        self._downstream_canceled = True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminal_reason(decision: ResolutionDecision) -> str:
    code = decision.error_code or ""
    if code == "stream_interrupted":
        return "stream_interrupted"
    if code in {"request_incompatible", "upstream_request_invalid"}:
        return "invalid_parameter"
    if code == "tool_incompatible":
        return "tool_incompatible"
    return "protocol_error"


def _degrade_persisted_provenance(record: dict) -> dict:
    degraded = dict(record)
    attempts = degraded.get("failed_attempts")
    if not isinstance(attempts, list):
        return degraded
    degraded_attempts = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            degraded_attempts.append(attempt)
            continue
        degraded_attempt = dict(attempt)
        reason = degraded_attempt.get("reason")
        if isinstance(reason, str):
            degraded_attempt["reason"] = RETIRED_PERSISTED_REASON_DEGRADATIONS.get(
                reason,
                reason,
            )
        degraded_attempts.append(degraded_attempt)
    degraded["failed_attempts"] = degraded_attempts
    return degraded


class BoundedProvenanceStore:
    """Atomic, bounded persistence for exact turn records."""

    def __init__(self, path: Path, *, max_entries: int = 500):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()

    @staticmethod
    def _read_path(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [_degrade_persisted_provenance(item) for item in payload if isinstance(item, dict)]

    def _read(self) -> list[dict]:
        return self._read_path(self.path)

    def _write_path(self, path: Path, records: list[dict]) -> None:
        write_state_document(path, records[-self.max_entries :])

    def _write(self, records: list[dict]) -> None:
        self._write_path(self.path, records)

    def put(self, record: dict) -> None:
        turn_id = str(record.get("turn_id") or "")
        if not turn_id:
            raise ValueError("turn_id is required")
        with self._lock:
            records = [
                item
                for item in self._read()
                if str(item.get("turn_id") or "") != turn_id
            ]
            records.append(record)
            self._write(records)

    def get(self, turn_id: str) -> Optional[dict]:
        with self._lock:
            return next(
                (
                    dict(item)
                    for item in reversed(self._read())
                    if item.get("turn_id") == turn_id
                ),
                None,
            )


class TurnCorrelationRegistry:
    """Correlate process credentials to the existing Workbench turn token."""

    def __init__(self, store: BoundedProvenanceStore):
        self.store = store
        self._lock = threading.RLock()
        self._scopes: dict[ScopeKey, ProcessScope] = {}
        self._token_scopes: dict[str, ScopeKey] = {}
        self._turn_scopes: dict[str, set[ScopeKey]] = {}
        self._traces: dict[str, TurnTrace] = {}

    @staticmethod
    def _scope_key(backend: str, process_scope: str) -> ScopeKey:
        if backend not in {"claude", "codex", "opencode"}:
            raise ValueError("unsupported backend")
        normalized = str(process_scope or "").strip()
        if not normalized:
            raise ValueError("process scope is required")
        return backend, normalized  # type: ignore[return-value]

    def credentials(
        self,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
    ) -> str:
        key = self._scope_key(backend, process_scope)
        normalized_turn_id = str(turn_id or "").strip() or None
        with self._lock:
            scope = self._scopes.get(key)
            if scope is None:
                scope = ProcessScope(token=secrets.token_urlsafe(32))
                self._scopes[key] = scope
                self._token_scopes[scope.token] = key

            # Frozen v3 has no discriminator for the shared OpenCode server.
            if normalized_turn_id is None or backend == "opencode":
                scope.untracked_use = True
                for active_turn_id in scope.active_turns:
                    trace = self._traces.get(active_turn_id)
                    if trace is not None:
                        trace.ambiguous = True
                return scope.token

            if scope.active_turns - {normalized_turn_id}:
                overlapping = scope.active_turns | {normalized_turn_id}
                scope.ambiguous_turns.update(overlapping)
                for active_turn_id in overlapping:
                    trace = self._traces.get(active_turn_id)
                    if trace is not None:
                        trace.ambiguous = True
            scope.active_turns.add(normalized_turn_id)
            self._turn_scopes.setdefault(normalized_turn_id, set()).add(key)
            return scope.token

    def authenticates(self, backend: str, token: str) -> bool:
        with self._lock:
            authorized = False
            for candidate, key in self._token_scopes.items():
                matches = secrets.compare_digest(candidate, token)
                authorized = authorized or (matches and key[0] == backend)
            return authorized

    def retire_scope(
        self,
        backend: str,
        process_scope: str,
        *,
        terminal_turn_id: Optional[str] = None,
    ) -> None:
        """Invalidate a process credential when its owning runtime is evicted."""

        key = self._scope_key(backend, process_scope)
        normalized_terminal_turn_id = str(terminal_turn_id or "").strip()
        with self._lock:
            scope = self._scopes.pop(key, None)
            if scope is None:
                return
            self._token_scopes.pop(scope.token, None)
            for turn_id in scope.active_turns:
                trace = self._traces.get(turn_id)
                terminal_is_exact = (
                    turn_id == normalized_terminal_turn_id
                    and trace is not None
                    and not trace.ambiguous
                    and not scope.untracked_use
                    and scope.active_turns == {turn_id}
                    and turn_id not in scope.ambiguous_turns
                    and bool(trace.failed_attempts or trace.terminal_error)
                )
                if terminal_is_exact:
                    turn_scopes = self._turn_scopes.get(turn_id)
                    if turn_scopes is not None:
                        turn_scopes.discard(key)
                elif trace is not None:
                    trace.ambiguous = True

    def _exact_turn(self, backend: str, token: str) -> tuple[str, ScopeKey] | None:
        key = self._token_scopes.get(token)
        if key is None or key[0] != backend:
            return None
        scope = self._scopes[key]
        if scope.untracked_use or len(scope.active_turns) != 1:
            for turn_id in scope.active_turns:
                trace = self._traces.get(turn_id)
                if trace is not None:
                    trace.ambiguous = True
            return None
        turn_id = next(iter(scope.active_turns))
        if turn_id in scope.ambiguous_turns:
            return None
        trace = self._traces.get(turn_id)
        if trace is not None and trace.ambiguous:
            return None
        return turn_id, key

    def begin_gateway_request(
        self,
        *,
        backend: str,
        token: str,
        requested_model_id: str,
    ) -> Optional[str]:
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None:
                return None
            turn_id, key = exact
            trace = self._traces.get(turn_id)
            if trace is None:
                trace = TurnTrace(
                    turn_id=turn_id,
                    agent=key[0],
                    requested_model_id=requested_model_id,
                    scope_key=key,
                )
                self._traces[turn_id] = trace
            elif (
                trace.gateway_model_id is not None
                and trace.gateway_model_id != requested_model_id
            ):
                trace.ambiguous = True
                self._scopes[key].ambiguous_turns.add(turn_id)
                return None
            return turn_id

    def prepare_gateway_turn(
        self,
        *,
        backend: str,
        token: str,
        turn_id: Optional[str] = None,
        requested_model_id: str,
        resolved_model_id: str,
        source_id: str,
        via_mapping: bool,
    ) -> None:
        """Retain routing independently from best-effort provenance attribution."""

        with self._lock:
            key = self._token_scopes.get(token)
            if key is None or key[0] != backend:
                return
            scope = self._scopes[key]
            normalized_turn_id = str(turn_id or "").strip()
            if normalized_turn_id:
                if normalized_turn_id not in scope.active_turns:
                    return
                route_turn_id = normalized_turn_id
            else:
                exact = self._exact_turn(backend, token)
                if exact is None:
                    return
                route_turn_id = exact[0]
            prepared = PreparedGatewayRoute(
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                source_id=source_id,
            )
            scope.route_callers.setdefault(resolved_model_id, set()).add(
                requested_model_id
            )
            existing = scope.prepared_routes.get(route_turn_id)
            if existing is not None and existing != prepared:
                scope.prepared_routes.pop(route_turn_id, None)
                scope.routing_conflicts.add(route_turn_id)
            elif route_turn_id not in scope.routing_conflicts:
                scope.prepared_routes[route_turn_id] = prepared

            exact = self._exact_turn(backend, token)
            if exact is None:
                return
            exact_turn_id, key = exact
            if exact_turn_id != route_turn_id:
                return
            trace = self._traces.setdefault(
                exact_turn_id,
                TurnTrace(
                    turn_id=exact_turn_id,
                    agent=key[0],
                    requested_model_id=requested_model_id,
                    scope_key=key,
                ),
            )
            if (
                trace.requested_model_id != requested_model_id
                or (
                    trace.gateway_source_id is not None
                    and trace.gateway_source_id != source_id
                )
                or (
                    trace.gateway_model_id is not None
                    and trace.gateway_model_id != resolved_model_id
                )
            ):
                trace.ambiguous = True
                self._scopes[key].ambiguous_turns.add(exact_turn_id)
                return
            trace.gateway_source_id = source_id
            trace.gateway_model_id = resolved_model_id

    def claim_gateway_request(
        self,
        *,
        backend: str,
        token: str,
        prepared_turn_id: Optional[str],
        prepared_attempt: Optional[AttemptIdentity] = None,
        gateway_model_id: str,
    ) -> GatewayRouting:
        """Route one gateway request and settle its attribution atomically.

        Where the request goes, whether a live turn owns it, and whether the
        attempt already opened for that turn still stands are one decision
        about one request, so they are made in one critical section. Split
        across two, a turn rotation lands in between: the turn that becomes
        the sole live one is then told a model it never asked for arrived on
        its token, which marks it ambiguous and drops the provenance of the
        request it goes on to make itself.

        The nested calls below re-enter `_lock`, which is an `RLock` for
        exactly this reason — reusing the two public mutators keeps one
        implementation of each rather than a `_locked` twin to keep in sync.
        """

        with self._lock:
            caller_model_id, claimed = self._route_gateway_model(
                backend=backend,
                token=token,
                gateway_model_id=gateway_model_id,
            )
            if prepared_turn_id is None:
                return GatewayRouting(caller_model_id, None)
            if claimed:
                owner = self.begin_gateway_request(
                    backend=backend,
                    token=token,
                    requested_model_id=gateway_model_id,
                )
                if owner != prepared_turn_id:
                    owner = None
                return GatewayRouting(caller_model_id, owner)
            # Routing was answered from the scope, so no live turn owns this
            # request and its own turn has already settled — there is nothing
            # left to attribute it to. The attempt opened for the live turn on
            # the way in therefore has to be given back: left behind, it
            # settles that turn as having canceled or interrupted a Hub attempt
            # it never made. It cannot be opened later instead, because `fail`
            # runs before this call and needs a turn to fail.
            #
            # Give back only what this request itself armed. A request that
            # armed nothing — because an attempt was already in flight on the
            # turn's one slot — has nothing to give back, and clearing that
            # attempt would lose its provenance outright: `finish_attempt`
            # returns on an empty slot with no identity to reconstruct from.
            if prepared_attempt is not None:
                self.clear_prepared_attempt(
                    prepared_turn_id,
                    only_if=prepared_attempt,
                )
            return GatewayRouting(caller_model_id, None)

    def _route_gateway_model(
        self,
        *,
        backend: str,
        token: str,
        gateway_model_id: str,
    ) -> tuple[Optional[str], bool]:
        """Resolve where this gateway request goes; call under `_lock`.

        Returns the caller model to route by, and whether a live turn claims
        it and may therefore be credited with the request.
        """

        key = self._token_scopes.get(token)
        if key is None or key[0] != backend:
            return None, True
        scope = self._scopes[key]
        if scope.routing_conflicts.intersection(scope.active_turns):
            # A live turn disagreed with itself about its own route. It owns
            # the resulting failure, so leave it attributable.
            return None, True
        # Only the caller model is a routing input: `ModelHubService.resolve`
        # takes no source, so two routes differing only in Source — the
        # ordinary shape of a route with fallback hops — are the same
        # routing answer and must not read as a disagreement.
        live_callers = {
            route.requested_model_id
            for turn_id, route in scope.prepared_routes.items()
            if turn_id in scope.active_turns
            and route.resolved_model_id == gateway_model_id
        }
        # A live turn claims this model exactly when one of its own prepared
        # routes resolves to it. Only then may the caller attribute the
        # request to that turn; every answer below comes from the scope
        # instead, and belongs to no open turn window.
        claimed = bool(live_callers)
        if len(live_callers) == 1:
            return next(iter(live_callers)), claimed
        if live_callers:
            return None, claimed
        # No live turn claims this model, yet the launched process is alive
        # on this token and keeps issuing requests: CLI tool loops,
        # agent-initiated continuations, and transport retries all land
        # here. Routing follows the process, so answer from every route the
        # scope has prepared rather than only the turn windows still open.
        # Routes held for *other* models say nothing about this one and must
        # not be read as ambiguity about it.
        scope_callers = scope.route_callers.get(gateway_model_id, set())
        if len(scope_callers) == 1:
            return next(iter(scope_callers)), claimed
        if scope_callers:
            return None, claimed
        return gateway_model_id if scope.untracked_use else None, claimed

    def gateway_terminalizer(
        self,
        *,
        backend: str,
        token: str,
    ) -> GatewayTurnTerminalizer:
        return GatewayTurnTerminalizer(
            self,
            backend=backend,
            token=token,
        )

    def _open_prepared_gateway_turn(
        self,
        *,
        backend: str,
        token: str,
    ) -> Optional[tuple[str, Optional[AttemptIdentity]]]:
        """Arm the launch identity for one gateway request.

        A turn holds one pending slot, but it describes one request, and
        concurrent requests from one process share the turn. So a preparation
        never displaces what is already there: whatever occupies the slot is an
        attempt some request has begun, and overwriting it substitutes this
        request's launch identity for the one that is actually in flight.
        Nothing is lost by declining — `_terminalize_gateway_exit` rebuilds the
        launch identity from the trace when the slot is empty or not ours.

        Returns the turn together with the identity armed, or `None` in that
        slot when an attempt was already in flight, so the caller gives back
        only what it armed itself.
        """

        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None:
                return None
            turn_id, _ = exact
            trace = self._traces.get(turn_id)
            if (
                trace is None
                or trace.gateway_source_id is None
                or trace.gateway_model_id is None
            ):
                return None
            if trace.pending_attempt is not None:
                return turn_id, None
            prepared = AttemptIdentity(
                source_id=trace.gateway_source_id,
                resolved_model_id=trace.gateway_model_id,
                channel="hub",
            )
            trace.pending_attempt = prepared
            return turn_id, prepared

    def clear_prepared_attempt(
        self,
        turn_id: Optional[str],
        *,
        only_if: Optional[AttemptIdentity] = None,
    ) -> None:
        """Remove the launch identity when cancellation precedes invocation.

        A turn holds one pending slot, but it describes one request, and
        concurrent gateway requests on one process share the turn. So a caller
        giving back a preparation it made itself passes it as `only_if`: the
        slot is cleared only while it still holds that exact preparation. The
        check is on object identity, not equality — a real attempt begun on the
        primary hop carries the same Source and model as the preparation it
        replaced, so `==` cannot tell "nobody has written here" from "the live
        request already did", and clearing the latter drops its provenance for
        good (`finish_attempt` returns on an empty slot and reconstructs
        nothing). `only_if=None` keeps the whole-turn behavior for callers that
        are ending the turn rather than one request.
        """

        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None and trace.pending_attempt is not None:
                if only_if is not None and trace.pending_attempt is not only_if:
                    return
                if trace.pending_attempt.channel == "hub":
                    trace.pending_attempt = None

    def _terminalize_gateway_exit(
        self,
        turn_id: Optional[str],
        *,
        reason: Literal[
            "invalid_parameter",
            "protocol_error",
            "engine_down",
        ] = "protocol_error",
        stream_started: bool,
        force: bool = False,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.ambiguous:
                return
            if not force and (
                trace.served is not None
                or trace.terminal_error is not None
                or trace.model_supply_state is not None
                or (
                    trace.pending_attempt is None
                    and bool(trace.failed_attempts)
                )
            ):
                return
            if reason == "engine_down":
                trace.pending_attempt = None
                trace.served = None
                trace.model_supply_state = None
                trace.blockers = []
                trace.terminal_error = {
                    "source_id": None,
                    "configured_model_id": None,
                    "channel": None,
                    "reason": reason,
                    "stream_started": stream_started,
                }
                return
            identity = trace.pending_attempt
            if identity is None and (
                trace.gateway_source_id is not None
                and trace.gateway_model_id is not None
            ):
                identity = AttemptIdentity(
                    source_id=trace.gateway_source_id,
                    resolved_model_id=trace.gateway_model_id,
                    channel="hub",
                )
            if identity is None or identity.channel != "hub":
                return
            trace.pending_attempt = None
            trace.served = None
            trace.terminal_error = {
                **identity.payload(),
                "reason": reason,
                "stream_started": stream_started,
            }

    def begin_native_attempt(
        self,
        *,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
        requested_model_id: str,
        source_id: str,
        resolved_model_id: str,
        via_mapping: bool,
    ) -> None:
        token = self.credentials(backend, process_scope, turn_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None or exact[0] != normalized_turn_id:
                return
            trace = self._traces.setdefault(
                normalized_turn_id,
                TurnTrace(
                    turn_id=normalized_turn_id,
                    agent=exact[1][0],
                    requested_model_id=requested_model_id,
                    scope_key=exact[1],
                ),
            )
            trace.pending_attempt = AttemptIdentity(
                source_id=source_id,
                resolved_model_id=resolved_model_id,
                channel="native_cli",
            )

    def mark_no_candidate(
        self,
        *,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
        requested_model_id: str,
        supply_state: SupplyState,
        blockers: Iterable[ExactHopBlocker] = (),
    ) -> None:
        token = self.credentials(backend, process_scope, turn_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            exact = self._exact_turn(backend, token)
            if exact is None or exact[0] != normalized_turn_id:
                return
            trace = self._traces.setdefault(
                normalized_turn_id,
                TurnTrace(
                    turn_id=normalized_turn_id,
                    agent=exact[1][0],
                    requested_model_id=requested_model_id,
                    scope_key=exact[1],
                ),
            )
            trace.model_supply_state = supply_state
            trace.blockers = [blocker.payload() for blocker in blockers]

    def mark_gateway_no_candidate(
        self,
        turn_id: Optional[str],
        supply_state: SupplyState,
        blockers: Iterable[ExactHopBlocker] = (),
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None:
                trace.pending_attempt = None
                trace.served = None
                trace.terminal_error = None
                trace.model_supply_state = supply_state
                trace.blockers = [blocker.payload() for blocker in blockers]

    def record_turn_outcome(
        self,
        turn_id: Optional[str],
        turn_outcome: TurnOutcomeProjectionInput | None,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None:
                trace.terminal_outcome = turn_outcome

    def begin_attempt(
        self,
        turn_id: Optional[str],
        *,
        source_id: str,
        resolved_model_id: str,
        channel: SupplyChannel,
        via_mapping: bool,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None:
                return
            trace.pending_attempt = AttemptIdentity(
                source_id=source_id,
                resolved_model_id=resolved_model_id,
                channel=channel,
            )

    def fail_native_attempt(
        self,
        turn_id: Optional[str],
        *,
        reason: ResolutionReason,
    ) -> None:
        """Convert an observed native terminal failure into a failed attempt."""

        normalized = str(turn_id or "").strip()
        if not normalized:
            return
        with self._lock:
            trace = self._traces.get(normalized)
            if (
                trace is None
                or trace.pending_attempt is None
                or trace.pending_attempt.channel != "native_cli"
            ):
                return
            identity = trace.pending_attempt
            trace.pending_attempt = None
            trace.served = None
            trace.terminal_error = None
            trace.failed_attempts.append(
                {**identity.payload(), "reason": reason}
            )

    def fail_hub_attempt(self, turn_id: Optional[str]) -> None:
        """Replace a gateway success rejected by the backend terminal result."""

        normalized = str(turn_id or "").strip()
        if not normalized:
            return
        with self._lock:
            trace = self._traces.get(normalized)
            if trace is None:
                return
            identity = trace.pending_attempt
            payload = (
                identity.payload()
                if identity is not None and identity.channel == "hub"
                else trace.served
            )
            if payload is None or payload.get("channel") != "hub":
                return
            trace.pending_attempt = None
            trace.served = None
            trace.terminal_error = {
                **payload,
                "reason": "protocol_error",
                "stream_started": True,
            }

    def finish_attempt(
        self,
        turn_id: Optional[str],
        *,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
    ) -> None:
        if turn_id is None:
            return
        if decision.error_code == "engine_down":
            self._terminalize_gateway_exit(
                turn_id,
                reason="engine_down",
                stream_started=outcome.stream_started,
                force=True,
            )
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.pending_attempt is None:
                return
            identity = trace.pending_attempt
            trace.pending_attempt = None
            if decision.action == "return":
                trace.served = identity.payload()
                trace.terminal_error = None
                return
            if decision.action == "fallback" and decision.reason is not None:
                trace.failed_attempts.append(
                    {**identity.payload(), "reason": decision.reason}
                )
                return
            if decision.action == "surface":
                trace.terminal_error = {
                    **identity.payload(),
                    "reason": _terminal_reason(decision),
                    "stream_started": outcome.stream_started,
                }

    def settle(self, turn_id: str, *, settled_by: Optional[str], ts: Optional[str] = None) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            trace = self._traces.pop(normalized_turn_id, None)
            scope_keys = self._turn_scopes.pop(normalized_turn_id, set())
            poisoned = False
            for key in scope_keys:
                scope = self._scopes.get(key)
                if scope is None:
                    continue
                poisoned = poisoned or scope.untracked_use
                scope.active_turns.discard(normalized_turn_id)
                scope.ambiguous_turns.discard(normalized_turn_id)
                scope.prepared_routes.pop(normalized_turn_id, None)
                scope.routing_conflicts.discard(normalized_turn_id)
            if trace is None or trace.ambiguous or poisoned:
                return

            served = trace.served
            terminal_error = trace.terminal_error
            canceled_attempt = None
            supply_state = None
            terminal_history_committed = (
                (
                    trace.terminal_outcome is not None
                    and trace.terminal_outcome.outcome != "canceled"
                )
                or trace.terminal_error is not None
                or trace.served is not None
            )
            if settled_by == SETTLED_BY_STOPPED and not terminal_history_committed:
                outcome = "canceled"
                canceled_attempt = (
                    trace.pending_attempt.payload()
                    if trace.pending_attempt is not None
                    else None
                )
                served = None
                terminal_error = None
            elif settled_by == SETTLED_BY_STOPPED:
                logger.info(
                    "Ignored stopped settlement after terminal Model Hub history was committed",
                    extra={"turn_id": normalized_turn_id},
                )
                if trace.model_supply_state is not None:
                    outcome = "no_candidate"
                    served = None
                    terminal_error = None
                    supply_state = trace.model_supply_state
                elif terminal_error is not None:
                    outcome = "failed_terminal"
                    served = None
                elif served is not None:
                    outcome = "served"
                elif trace.failed_attempts:
                    outcome = "exhausted"
                else:
                    return
            elif trace.model_supply_state is not None:
                outcome = "no_candidate"
                served = None
                terminal_error = None
                supply_state = trace.model_supply_state
            elif settled_by in {
                SETTLED_BY_NO_TERMINAL_RESULT,
                SETTLED_BY_BACKEND_REFRESH,
            }:
                interrupted_attempt = (
                    trace.pending_attempt.payload()
                    if trace.pending_attempt is not None
                    else served
                )
                if terminal_error is None and interrupted_attempt is None:
                    if not trace.failed_attempts:
                        return
                    outcome = "exhausted"
                else:
                    outcome = "failed_terminal"
                    served = None
                    terminal_error = terminal_error or {
                        **interrupted_attempt,
                        "reason": "stream_interrupted",
                        "stream_started": True,
                    }
            elif (
                settled_by == SETTLED_BY_TERMINAL_RESULT
                and trace.pending_attempt is not None
                and trace.pending_attempt.channel == "native_cli"
            ):
                outcome = "served"
                served = trace.pending_attempt.payload()
                terminal_error = None
            elif terminal_error is not None:
                outcome = "failed_terminal"
                served = None
            elif served is not None:
                outcome = "served"
            elif trace.failed_attempts:
                outcome = "exhausted"
            else:
                return

            self.store.put(
                {
                    "contract_version": 6,
                    "turn_id": normalized_turn_id,
                    "ts": ts or _utc_now_iso(),
                    "agent": trace.agent,
                    "requested_model_id": trace.requested_model_id,
                    "outcome": outcome,
                    "failed_attempts": list(trace.failed_attempts),
                    "served": served,
                    "terminal_error": terminal_error,
                    "canceled_attempt": canceled_attempt,
                    "model_supply_state": supply_state,
                    "blockers": (
                        list(trace.blockers)
                        if outcome == "no_candidate"
                        else []
                    ),
                }
            )
