"""Exact-attribution turn provenance for process-scoped Model Hub traffic."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from core.agent_core.ai.errors import redact_provider_text
from config.v2_config import MODEL_HUB_BACKENDS, ModelHubConfig
from modules.agents.catalog import display_name_for_backend
from core.os_errors import format_os_errno

from core.run_settlement import (
    SETTLED_BY_BACKEND_DISABLED,
    SETTLED_BY_BACKEND_REFRESH,
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_STOPPED,
    SETTLED_BY_TERMINAL_RESULT,
)
from vibe.i18n import t as i18n_t

from .adapter import RawCallOutcome, sanitize_response_correlation_ids
from .classification import (
    UPSTREAM_MACHINE_ERROR_CODES,
    ResolutionDecision,
    ResolutionReason,
    machine_error_codes,
)
from .events import (
    EVENT_REASON_AUTHORITY,
    RETIRED_PERSISTED_REASON_DEGRADATIONS,
    SOURCE_DETAIL_EVENT_REASONS,
    bounded_diagnostic_detail,
    event_reason_label,
)
from .hosted_turn import TurnHostedTools
from .resolver import BackendName, ModelHubTurnResolution, parse_model_hub_timestamp, source_eligible_for_backend
from .state_file import write_state_document


SupplyChannel = Literal["native_cli", "hub"]
# A backend's typed failure kind is a member of that backend's closed vocabulary
# (Vibey: ``NAMED_ERROR_KINDS``), never free text: the record keeps only that shape.
_BACKEND_ERROR_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
SupplyState = Literal["waiting", "interrupted"]
ScopeKey = tuple[BackendName, str]
logger = logging.getLogger(__name__)

SERVED_HOP_HEADER = "x-avibe-served-hop"
SERVED_HOP_HEADER_MAX_BYTES = 4096


class ServedHopHeaderTooLarge(ValueError):
    """The exact origin cannot be transported in the bounded consumer header."""


class PreparedGatewayRouteConflict(ValueError):
    """A Vibey launch would replace another request's still-owned route."""


@dataclass(frozen=True)
class HopOrigin:
    """Source identity captured at admission, before mutable config can change."""

    provider: str
    api: str
    model: str
    # The serving Source: two Sources can share an origin. Equality stays the
    # origin's; opaque payloads are also bound to the Source's account (C-6 item
    # 6), which the failover strip compares on its own.
    source: str = field(compare=False)
    # The launch Source's credential-free identity (``identity_digest``), taken
    # from the snapshot the origin came from. In memory only: never in
    # ``payload()``, the served-hop header, or persisted provenance.
    identity: str = field(default="", compare=False, repr=False)

    def payload(self) -> dict[str, str]:
        return {"provider": self.provider, "api": self.api, "model": self.model}

    def response_headers(self) -> dict[str, str]:
        # JSON escapes keep arbitrary Unicode model ids safe in HTTP headers.
        value = json.dumps(
            {**self.payload(), "source": self.source},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        # ensure_ascii makes character count equal to wire byte count.
        if len(value) > SERVED_HOP_HEADER_MAX_BYTES:
            raise ServedHopHeaderTooLarge("Served-hop header exceeds its transport bound")
        return {SERVED_HOP_HEADER: value}


# The user ended the turn: a Stop, or turning its backend off. Its outcome is
# canceled, never a provider stream failure.
_USER_CANCELLATIONS = frozenset({SETTLED_BY_STOPPED, SETTLED_BY_BACKEND_DISABLED})


@dataclass(frozen=True)
class AttemptIdentity:
    source_id: str
    resolved_model_id: str
    channel: SupplyChannel
    stripped_reasoning_efforts: tuple[str, ...] = ()
    declared_reasoning_efforts: tuple[str, ...] = ()
    origin: HopOrigin | None = None

    def payload(self) -> dict:
        payload = {
            "source_id": self.source_id,
            "configured_model_id": self.resolved_model_id,
            "channel": self.channel,
        }
        if self.stripped_reasoning_efforts:
            payload["stripped_reasoning_efforts"] = list(
                self.stripped_reasoning_efforts
            )
            payload["declared_reasoning_efforts"] = list(
                self.declared_reasoning_efforts
            )
        if self.origin is not None:
            payload["origin"] = self.origin.payload()
        return payload


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
            if inspection.backoff or inspection.recovery == "in_flight":
                reason = "network" if inspection.backoff else "cooldown"
            elif inspection.cooldown:
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
class TurnUpstreamFailure:
    """The last upstream refusal or connection failure behind a failed turn."""

    source: str
    # None when the transport did not expose an upstream HTTP status. A
    # protocol-level failure can still have answered through an HTTP 2xx
    # envelope, so this is not by itself a connection verdict.
    http_status: int | None
    # Redacted and bounded by the engine client; display only.
    detail: str
    connection_error: bool = False


@dataclass(frozen=True)
class TurnOutcomeRenderingRule:
    outcome: str
    discriminator: str
    copy_keys: tuple[tuple[str, str | None], ...]
    # Appended to the summary when the turn carries its last upstream failure:
    # an upstream refusal, or a connection that no upstream answered.
    upstream_failure_key: str | None = None
    upstream_failure_without_status_key: str | None = None
    connection_failure_key: str | None = None
    # Only a Models change clears this refusal: the same input fails the same way until
    # then, so it waits for an explicit retry instead of the automatic startup retries.
    holds_input: bool = False


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
        upstream_failure_key="modelHub.launch.last_upstream_failure",
        upstream_failure_without_status_key="modelHub.launch.last_upstream_failure_without_status",
        connection_failure_key="modelHub.launch.last_connection_failure",
    ),
    "turn.request_nonfallback": TurnOutcomeRenderingRule(
        outcome="failed_terminal",
        discriminator="request_nonfallback",
        copy_keys=(
            ("default", "modelHub.launch.request_incompatible"),
            ("upstream_detail", "modelHub.launch.request_incompatible_detail"),
        ),
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
        upstream_failure_key="modelHub.launch.last_upstream_failure",
        upstream_failure_without_status_key="modelHub.launch.last_upstream_failure_without_status",
        connection_failure_key="modelHub.launch.last_connection_failure",
    ),
    "turn.no_candidate.unconfigured": TurnOutcomeRenderingRule(
        outcome="no_candidate",
        discriminator="route_unconfigured",
        copy_keys=(("interrupted", "modelHub.launch.route_unconfigured"),),
        holds_input=True,
    ),
    "turn.no_candidate.unlisted": TurnOutcomeRenderingRule(
        outcome="no_candidate",
        discriminator="model_unlisted",
        copy_keys=(("interrupted", "modelHub.launch.model_unlisted"),),
        holds_input=True,
    ),
    "turn.no_candidate.blocked": TurnOutcomeRenderingRule(
        outcome="no_candidate",
        discriminator="blocked_supply_state",
        copy_keys=(
            ("waiting", "modelHub.launch.waiting"),
            ("waiting_without_retry", "modelHub.launch.waiting_without_retry"),
            ("interrupted", "modelHub.launch.interrupted"),
        ),
        upstream_failure_key="modelHub.launch.last_upstream_failure",
        upstream_failure_without_status_key="modelHub.launch.last_upstream_failure_without_status",
        connection_failure_key="modelHub.launch.last_connection_failure",
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
    # Redacted, bounded upstream error text for this turn's reply only.
    upstream_detail: str | None = None
    # An OS-generated reason, separate from summary copy and upstream text.
    local_error_detail: str | None = None
    # The last fallback-class upstream refusal, appended to supply copy.
    last_upstream_failure: TurnUpstreamFailure | None = None


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
    if projection.upstream_detail and "upstream_detail" in copy_keys:
        return "upstream_detail"
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
    upstream_detail: str | None = None,
    last_upstream_failure: TurnUpstreamFailure | None = None,
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
        upstream_detail=(
            upstream_detail
            if "upstream_detail" in variants and upstream_detail
            else None
        ),
        last_upstream_failure=(
            last_upstream_failure if rule.upstream_failure_key is not None else None
        ),
    )
    if _turn_outcome_variant(projection, rule) not in dict(rule.copy_keys):
        raise TurnOutcomeProductionError(
            "Turn outcome production is missing its required rendering fact"
        )
    return projection


def no_candidate_decision(
    config: ModelHubConfig,
    resolution: ModelHubTurnResolution,
) -> str:
    """Choose the matrix row for a turn that has no runnable hop.

    An id the backend's model list does not hold has an empty effective Route
    unless a manual Route row was retained for it, so an empty chain for such
    an id is the list's answer rather than a Route the user can configure.
    """

    if not resolution.route_unconfigured:
        return "turn.no_candidate.blocked"
    requested = resolution.requested_model
    if requested and all(
        model.id != requested for model in config.agents[resolution.backend].models
    ):
        return "turn.no_candidate.unlisted"
    return "turn.no_candidate.unconfigured"


def is_unlisted_model_outcome(projection: TurnOutcomeProjectionInput | None) -> bool:
    """Whether a turn was refused because its backend's list does not hold its model."""

    rule = TURN_OUTCOME_RENDERING_AUTHORITY["turn.no_candidate.unlisted"]
    return projection is not None and (projection.outcome, projection.discriminator) == (
        rule.outcome,
        rule.discriminator,
    )


def outcome_holds_input(projection: TurnOutcomeProjectionInput | None) -> bool:
    """Whether a refused turn's input waits for an explicit retry, as its matrix row says."""

    return projection is not None and any(
        rule.holds_input
        and (rule.outcome, rule.discriminator) == (projection.outcome, projection.discriminator)
        for rule in TURN_OUTCOME_RENDERING_AUTHORITY.values()
    )


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
        hop
        for hop in resolution.inspected_hops
        if hop.temporary_blocker and not hop.runnable
    )
    blockers: list[TurnSupplyBlocker] = []
    for inspection in resolution.inspected_hops:
        if inspection.runnable:
            continue
        source = inspection.source
        reason = inspection.reason
        if reason not in EVENT_REASON_AUTHORITY and source is not None:
            reason = (
                inspection.cooldown_reason
                if inspection.cooldown_reason in EVENT_REASON_AUTHORITY
                else SOURCE_DETAIL_EVENT_REASONS.get(source.state.detail_key or "", reason)
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
        source=", ".join(hop.source.display_name for hop in cooling if hop.source is not None),
        retry_at=min(
            (hop.retry_at for hop in cooling if hop.retry_at),
            key=parse_model_hub_timestamp,
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
            "detail": projection.upstream_detail or "",
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
    if params.get("backend"):
        params["backend"] = display_name_for_backend(params["backend"])
    text = i18n_t(copy.key, language, **params)
    failure = projection.last_upstream_failure
    rule = _turn_outcome_rule(projection)
    suffix_key = (
        rule.connection_failure_key
        if failure is not None and failure.connection_error
        else (
            rule.upstream_failure_key
            if failure is not None and failure.http_status is not None
            else rule.upstream_failure_without_status_key
        )
    )
    if failure is not None and suffix_key is not None:
        text = " ".join((
            text,
            i18n_t(
                suffix_key, language,
                source=failure.source, status=failure.http_status, detail=failure.detail,
            ),
        ))
    return text


# The request id for a turn's own single attempt: the native CLI path, where
# the turn and the request really are one thing.
TURN_REQUEST = "turn"


@dataclass
class TurnTrace:
    turn_id: str
    agent: BackendName
    requested_model_id: str
    scope_key: ScopeKey
    failed_attempts: list[dict] = field(default_factory=list)
    served: Optional[dict] = None
    terminal_error: Optional[dict] = None
    # One entry per in-flight request, keyed by request id, in arrival order.
    # An attempt identity describes a request, not a turn, and one launched
    # process issues concurrent requests on one turn's credentials — so a
    # single slot has each arrival overwrite the identity of whichever request
    # is actually awaiting an upstream result, and the loser settles with no
    # record of what served it.
    pending_attempts: dict[str, AttemptIdentity] = field(default_factory=dict)
    # Attempts can settle before their HTTP request exits, or be absent while
    # recovery waits. Route replacement must wait for the whole request.
    gateway_requests: set[str] = field(default_factory=set)
    model_supply_state: Optional[SupplyState] = None
    blockers: list[dict] = field(default_factory=list)
    gateway_source_id: Optional[str] = None
    gateway_request_model_id: Optional[str] = None
    gateway_model_id: Optional[str] = None
    ambiguous: bool = False
    terminal_outcome: TurnOutcomeProjectionInput | None = None
    admission_closed: bool = False
    # Both causes veto mutation. Backend acknowledgments keep terminal copy
    # readable; explicit user cancellation suppresses live copy as before.
    outcome_frozen: Literal["backend_failure", "user_cancellation"] | None = None
    recovery_requests: dict[str, dict] = field(default_factory=dict)
    # The request whose surfaced attempt set ``terminal_error``, and that exact
    # value: a handed-back request withdraws only its own terminal.
    terminal_error_by: Optional[tuple[str, dict]] = None
    # Vibey: the hosted tools the turn's first resolution froze, with each
    # search tool's executor order. Written once; settings apply next turn.
    hosted_tools: Optional[TurnHostedTools] = None
    # Vibey: per Source, the identity digest the turn's native content is bound
    # to, claimed by its first admitted pinned request. Each entry is written once.
    pinned_identities: dict[str, str] = field(default_factory=dict)

    @property
    def pending_attempt(self) -> Optional[AttemptIdentity]:
        """The in-flight attempt this turn has been waiting on longest.

        Turn-terminal readers — cancellation, interruption, settlement — need
        one identity for a turn that may hold several. The oldest is the only
        stable choice: entries are removed as their requests settle, so
        whatever is still first arrived before all the others and cannot be
        displaced by a later one.
        """

        for identity in self.pending_attempts.values():
            return identity
        return None


@dataclass
class ProcessScope:
    token: str
    active_turns: set[str] = field(default_factory=set)
    ambiguous_turns: set[str] = field(default_factory=set)
    prepared_routes: dict[str, "PreparedGatewayRoute"] = field(default_factory=dict)
    routing_conflicts: set[str] = field(default_factory=set)
    untracked_use: bool = False
    request_scoped: bool = False
    # The credential each route launched from this scope authenticates with,
    # one per distinct route rather than one per launch. `token` identifies
    # the process; these identify a route within it, which is what routing
    # actually needs: an upstream model id is not unique across routes, so a
    # credential that names only the process cannot answer which route a
    # request for that model belongs to.
    route_tokens: dict["PreparedGatewayRoute", str] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedGatewayRoute:
    requested_model_id: str
    resolved_model_id: str
    source_id: str
    gateway_request_model_id: str
    primary_origin: HopOrigin | None = None
    # Vibey only: the primary Source's identity at launch. ``HopOrigin`` compares
    # without it, but a route under another identity is another route, so its
    # credential carries the launch identity the gateway compares against.
    primary_identity: str = ""


@dataclass(frozen=True)
class GatewayCredential:
    """What a gateway credential answers for: a process, and maybe a route."""

    scope_key: ScopeKey
    route: Optional[PreparedGatewayRoute] = None


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
        request_metadata: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._backend = backend
        self._token = token
        self._request_metadata = (
            dict(request_metadata) if isinstance(request_metadata, Mapping) else None
        )
        # One gateway request, one identity. Every attempt this terminalizer
        # records is filed under it, so concurrent requests on one turn cannot
        # overwrite each other's provenance.
        self._request_id = secrets.token_hex(8)
        self.turn_id = registry._open_prepared_gateway_turn(
            backend=backend,
            token=token,
            request_id=self._request_id,
            request_metadata=self._request_metadata,
        )
        self._prepared_turn_id = self.turn_id
        self.primary_origin = registry.gateway_primary_origin(
            backend=backend, token=token, request_metadata=self._request_metadata,
        )
        self._stream_started = False
        self._attempt_started = False
        self._downstream_canceled = False
        self._recovery_closed = False
        # This request's exit, staged so it commits as one registry write.
        self._staged_exit: dict | None = None
        self._exit_committed = False
        self.on_attribution_released: Callable[[], None] | None = None

    def __enter__(self) -> "GatewayTurnTerminalizer":
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        try:
            if self._downstream_canceled or exc_type is asyncio.CancelledError:
                return
            self.commit_exit()
            if self._exit_committed:
                # A classified exit is this request's whole ending; the
                # unclassified fallback below is only for requests without one.
                return
            self._registry._terminalize_gateway_exit(
                self.turn_id,
                request_id=self._request_id,
                stream_started=self._stream_started,
            )
        finally:
            self._registry.close_gateway_request(self._prepared_turn_id, request_id=self._request_id)

    def resolution_model(self, gateway_model_id: str) -> Optional[str]:
        """Return the uniquely prepared caller model for this gateway request."""

        routing = self._registry.claim_gateway_request(
            backend=self._backend,
            token=self._token,
            prepared_turn_id=self.turn_id,
            request_id=self._request_id,
            gateway_model_id=gateway_model_id,
            request_metadata=self._request_metadata,
        )
        self.turn_id = routing.owner_turn_id
        if self.turn_id is None:
            self._registry.close_gateway_request(self._prepared_turn_id, request_id=self._request_id)
        if self.turn_id is None and self.on_attribution_released is not None:
            release, self.on_attribution_released = self.on_attribution_released, None
            release()
        return routing.caller_model_id

    def fail(
        self,
        reason: Literal["invalid_parameter", "protocol_error"],
    ) -> None:
        self._staged_exit = {**(self._staged_exit or {}), "fail_reason": reason}

    def pinned_hop(self, source_id: str, model_id: str | None) -> tuple[str, str | None]:
        """The exact hop an ``x-avibe-pin-source`` request was rendered for, from explicit or immutable data.

        A continuation names its model (``x-avibe-pin-model``: the served-hop
        model of the response it continues). Otherwise the pin is the launch's
        primary hop, whose origin this route credential fixes. A pin that is
        neither has no model and matches no hop. Nothing turn-wide is read: a
        turn may have concurrent requests.
        """

        if model_id is None and self.primary_origin is not None and self.primary_origin.source == source_id:
            model_id = self.primary_origin.model
        return source_id, model_id

    def hand_back(self) -> None:
        """End this request without an exit of its own: its caller re-sends the content.

        A pinned Vibey request the gateway cannot serve (``pin_unavailable``)
        is re-rendered and sent again in the same turn. Attempts it made stay
        recorded; the turn settles on the request that follows.
        """

        self._registry.withdraw_terminal_error(self.turn_id, request_id=self._request_id)
        self._registry.clear_prepared_attempt(self.turn_id, request_id=self._request_id)
        self._staged_exit = None
        self._exit_committed = True

    def engine_down(self, *, local_error_detail: str | None = None) -> None:
        self._registry._terminalize_gateway_exit(
            self.turn_id,
            request_id=self._request_id,
            reason="engine_down",
            local_error_detail=local_error_detail,
            stream_started=self._stream_started,
            force=True,
        )

    def mark_no_candidate(
        self,
        supply_state: SupplyState,
        blockers: Iterable[ExactHopBlocker] = (),
    ) -> None:
        self._staged_exit = {
            **(self._staged_exit or {}), "no_candidate": (supply_state, tuple(blockers)),
        }

    def begin_attempt(
        self,
        *,
        source_id: str,
        resolved_model_id: str,
        channel: SupplyChannel,
        via_mapping: bool,
        stripped_reasoning_efforts: tuple[str, ...] = (),
        declared_reasoning_efforts: tuple[str, ...] = (),
        origin: HopOrigin | None = None,
    ) -> None:
        self._attempt_started = True
        self._registry.begin_attempt(
            self.turn_id,
            source_id=source_id,
            resolved_model_id=resolved_model_id,
            channel=channel,
            via_mapping=via_mapping,
            stripped_reasoning_efforts=stripped_reasoning_efforts,
            declared_reasoning_efforts=declared_reasoning_efforts,
            origin=origin,
            request_id=self._request_id,
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
            request_id=self._request_id,
        )

    def mark_stream_started(self) -> None:
        self._stream_started = True

    def update_recovery(self, snapshot: dict | None) -> None:
        if self._recovery_closed:
            return
        if snapshot is None:
            self._recovery_closed = True
        self._registry.update_recovery(
            self.turn_id, backend=self._backend, request_id=self._request_id, snapshot=snapshot,
        )

    def record_turn_outcome(
        self,
        turn_outcome: TurnOutcomeProjectionInput | None,
    ) -> None:
        """Keep the settlement projection attached to the correlated turn event."""

        self.commit_exit(turn_outcome, has_outcome=True)

    def commit_exit(
        self,
        turn_outcome: TurnOutcomeProjectionInput | None = None,
        *,
        has_outcome: bool = False,
    ) -> None:
        """Commit this request's staged exit and projection as one write."""

        staged, self._staged_exit = self._staged_exit, None
        if staged is None and not has_outcome:
            return
        self._exit_committed = True
        self._registry.commit_gateway_exit(
            self.turn_id,
            request_id=self._request_id,
            stream_started=self._stream_started,
            turn_outcome=turn_outcome,
            has_outcome=has_outcome,
            **(staged or {}),
        )

    def mark_downstream_canceled(self) -> None:
        """Clear a prepared-only attempt before the outer stopped settlement."""

        if not self._attempt_started:
            self._registry.clear_prepared_attempt(
                self.turn_id,
                request_id=self._request_id,
            )
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


def _response_correlation_payload(values: object) -> dict[str, list[dict[str, str]]]:
    """Serialize only the adapter's bounded, allowlisted response IDs."""

    ids = sanitize_response_correlation_ids(values)
    if not ids:
        return {}
    return {
        "response_correlation_ids": [
            {"header": header, "value": value}
            for header, value in ids
        ]
    }


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

    def latest_for_model(self, backend: str, model_id: str) -> Optional[dict]:
        with self._lock:
            return next(
                (
                    dict(record)
                    for record in reversed(self._read())
                    if record.get("agent") == backend
                    and record.get("requested_model_id") == model_id
                ),
                None,
            )


class TurnCorrelationRegistry:
    """Correlate process credentials to the existing Workbench turn token."""

    def __init__(
        self, store: BoundedProvenanceStore,
        *, on_recovery_changed: Callable[[str], None] | None = None,
    ):
        self.store = store
        self._lock = threading.RLock()
        self._scopes: dict[ScopeKey, ProcessScope] = {}
        self._credentials: dict[str, GatewayCredential] = {}
        self._turn_scopes: dict[str, set[ScopeKey]] = {}
        self._traces: dict[str, TurnTrace] = {}
        self.on_recovery_changed = on_recovery_changed

    def _readable_trace(
        self, turn_id: str, backend: str | None = None, *,
        allow_closed: bool = False, allow_acknowledged: bool = False,
    ) -> TurnTrace | None:
        """Called under the registry lock; attribution is never inferred."""

        trace = self._traces.get(turn_id)
        if (
            trace is None or trace.ambiguous
            or (trace.outcome_frozen and not (allow_acknowledged and trace.outcome_frozen == "backend_failure"))
            or (trace.admission_closed and not allow_closed)
            or (backend is not None and trace.agent != backend)
        ):
            return None
        keys = self._turn_scopes.get(turn_id, set())
        if not keys or any(
            key[0] != trace.agent or (scope := self._scopes.get(key)) is None
            or scope.untracked_use or turn_id in scope.ambiguous_turns
            or turn_id not in scope.active_turns
            for key in keys
        ):
            return None
        return trace

    def terminal_projection(self, turn_id: str, *, backend: str) -> TurnOutcomeProjectionInput | None:
        with self._lock:
            trace = self._readable_trace(turn_id, backend, allow_closed=True, allow_acknowledged=True)
            if (
                trace is None or (trace.pending_attempts and not trace.outcome_frozen) or trace.terminal_outcome is None
                or trace.terminal_outcome.outcome in {"served", "canceled"}
            ):
                return None
            return trace.terminal_outcome

    def recovery_snapshot(self, turn_id: str) -> list[dict]:
        with self._lock:
            trace = self._readable_trace(turn_id)
            return [dict(item) for item in trace.recovery_requests.values()] if trace else []

    def update_recovery(
        self, turn_id: str | None, *, backend: str, request_id: str, snapshot: dict | None,
    ) -> None:
        if turn_id is None:
            return
        changed = False
        with self._lock:
            trace = self._readable_trace(turn_id, backend)
            if trace is None:
                return
            previous = trace.recovery_requests.get(request_id)
            current = {**snapshot, "request_id": request_id} if snapshot is not None else None
            if previous != current:
                changed = True
                if current is None:
                    trace.recovery_requests.pop(request_id, None)
                else:
                    trace.recovery_requests[request_id] = current
        if changed and self.on_recovery_changed is not None:
            self._notify_recovery_changed(turn_id)

    def _notify_recovery_changed(self, turn_id: str) -> None:
        if self.on_recovery_changed is not None:
            try:
                self.on_recovery_changed(turn_id)
            except Exception:
                logger.exception("Could not publish Model Hub recovery activity")

    @staticmethod
    def _scope_key(backend: str, process_scope: str) -> ScopeKey:
        if backend not in MODEL_HUB_BACKENDS:
            raise ValueError("unsupported backend")
        normalized = str(process_scope or "").strip()
        if not normalized:
            raise ValueError("process scope is required")
        return backend, normalized  # type: ignore[return-value]

    @staticmethod
    def _request_identity(
        request_metadata: Mapping[str, str] | None,
    ) -> tuple[str, str] | None:
        if not isinstance(request_metadata, Mapping):
            return None
        route_id = request_metadata.get("avibe_route_id")
        turn_id = request_metadata.get("avibe_turn_id")
        if (
            not isinstance(route_id, str)
            or not route_id.strip()
            or not isinstance(turn_id, str)
            or (turn_id != "" and not turn_id.strip())
        ):
            return None
        return route_id, turn_id

    @staticmethod
    def _register_explicit_turn(
        *,
        key: ScopeKey,
        scope: ProcessScope,
        turn_id: str | None,
        turn_scopes: dict[str, set[ScopeKey]],
    ) -> None:
        if turn_id is None:
            return
        scope.active_turns.add(turn_id)
        turn_scopes.setdefault(turn_id, set()).add(key)

    def credentials(
        self,
        backend: str,
        process_scope: str,
        turn_id: Optional[str],
        *,
        request_scoped: bool = False,
    ) -> str:
        key = self._scope_key(backend, process_scope)
        normalized_turn_id = str(turn_id or "").strip() or None
        with self._lock:
            scope = self._scopes.get(key)
            if scope is None:
                scope = ProcessScope(token=secrets.token_urlsafe(32))
                self._scopes[key] = scope
                self._credentials[scope.token] = GatewayCredential(scope_key=key)

            if backend in {"codex", "opencode"} and request_scoped and not scope.request_scoped:
                for active_turn_id in scope.active_turns:
                    trace = self._traces.get(active_turn_id)
                    if trace is not None and scope.untracked_use:
                        trace.ambiguous = True
                scope.request_scoped = True
                scope.untracked_use = False
                for route_token in scope.route_tokens.values():
                    self._credentials.pop(route_token, None)

            # Explicit native scopes never infer ownership from a sole active
            # turn. HTTP requests carry the route metadata when the process
            # multiplexes more than one Source.
            if backend in {"codex", "opencode"} and scope.request_scoped:
                self._register_explicit_turn(
                    key=key,
                    scope=scope,
                    turn_id=normalized_turn_id,
                    turn_scopes=self._turn_scopes,
                )
                return scope.token

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
            for candidate, credential in self._credentials.items():
                matches = secrets.compare_digest(candidate, token)
                authorized = authorized or (
                    matches and credential.scope_key[0] == backend
                )
            return authorized

    def _credential(self, backend: str, token: str) -> Optional[GatewayCredential]:
        """Resolve a token to what it answers for; call under `_lock`."""

        credential = self._credentials.get(token)
        if credential is None or credential.scope_key[0] != backend:
            return None
        return credential

    def _route_credential(
        self,
        key: ScopeKey,
        scope: ProcessScope,
        route: PreparedGatewayRoute,
    ) -> str:
        """Return the credential a launch on this route authenticates with.

        One credential per distinct route, not per launch: `authenticates`
        compares against every credential in constant time, so the cost is
        bounded by the routes a process actually uses rather than by how many
        times it is launched. Re-launching the same route is the same routing
        answer and reuses the same credential.
        """

        token = scope.route_tokens.get(route)
        if token is None:
            token = secrets.token_urlsafe(32)
            scope.route_tokens[route] = token
            if not scope.request_scoped:
                self._credentials[token] = GatewayCredential(
                    scope_key=key,
                    route=route,
                )
        return token

    def claim_pinned_identity(
        self, turn_id: Optional[str], source_id: str, identity: str, continuation: bool,
    ) -> bool:
        """Whether a turn's native content may go to this Source under ``identity``; binds the first claim.

        The turn's first claim for a Source binds its identity, and later ones
        must match it. A continuation carries the native items of a response
        already served, so it needs an identity already bound. A request without
        a live turn has none to bind to.
        """

        with self._lock:
            trace = self._traces.get(str(turn_id or "").strip())
            if trace is None:
                return False
            bound = trace.pinned_identities.get(source_id)
            if bound is None:
                if continuation:
                    return False
                trace.pinned_identities[source_id] = identity
                return True
            return bound == identity

    def turn_hosted_tools(self, turn_id: Optional[str]) -> TurnHostedTools | None:
        """The hosted tools a turn's first resolution froze, or None before it has them."""

        normalized = str(turn_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            trace = self._traces.get(normalized)
            return trace.hosted_tools if trace is not None else None

    def live_hosted_turn(
        self, *, backend: str, token: str, request_metadata: Mapping[str, str] | None,
    ) -> tuple[str, TurnHostedTools | None] | None:
        """The live turn a request belongs to and the hosted tools it froze; None without a live turn.

        Only the turn the request names, while it holds the credential's route
        and still admits work, is live: a request from a finished, superseded
        or unnamed turn, or from a turn being stopped or finalized, cannot
        start a call.
        """

        with self._lock:
            owner = self._request_turn(backend, token, request_metadata)
            if owner is None:
                return None
            trace = self._traces.get(owner[0])
            return owner[0], trace.hosted_tools if trace is not None else None

    def gateway_primary_origin(
        self,
        *,
        backend: str,
        token: str,
        request_metadata: Mapping[str, str] | None = None,
    ) -> HopOrigin | None:
        """Read the immutable launch origin, including an explicit route handle."""

        with self._lock:
            credential = self._credential(backend, token)
            if credential is None:
                return None
            if credential.route is not None:
                return credential.route.primary_origin
            explicit = self._explicit_route(
                backend=backend, token=token, request_metadata=request_metadata,
            )
            return explicit[2].primary_origin if explicit is not None else None

    def _explicit_route(
        self,
        *,
        backend: str,
        token: str,
        request_metadata: Mapping[str, str] | None,
    ) -> tuple[ScopeKey, ProcessScope, PreparedGatewayRoute, str] | None:
        credential = self._credential(backend, token)
        if credential is None:
            return None
        key = credential.scope_key
        scope = self._scopes.get(key)
        if scope is None or not scope.request_scoped:
            return None
        identity = self._request_identity(request_metadata)
        if identity is None:
            return None
        route_id, _turn_id = identity
        route = next(
            (
                candidate
                for candidate, candidate_id in scope.route_tokens.items()
                if candidate_id == route_id
            ),
            None,
        )
        if route is None:
            return None
        return key, scope, route, route_id

    @staticmethod
    def _explicit_turn_owns_route(
        scope: ProcessScope,
        *,
        turn_id: str,
        route: PreparedGatewayRoute,
    ) -> bool:
        return (
            turn_id in scope.active_turns
            and turn_id not in scope.ambiguous_turns
            and turn_id not in scope.routing_conflicts
            and scope.prepared_routes.get(turn_id) == route
        )

    def _request_turn(
        self,
        backend: str,
        token: str,
        request_metadata: Mapping[str, str] | None,
    ) -> tuple[str, ScopeKey] | None:
        """The live turn one gateway request on a route credential belongs to; call under `_lock`.

        Vibey names its turn on every request (``VIBEY_TURN_HEADER``), because
        its credential answers for a route that later turns of the session
        reuse: the named turn owns the request only while it holds that route
        and still admits work, so a request from a finished turn is never
        credited to its successor. Other credential-bound clients name no
        turn, and their sole live turn is inferred.
        """

        if backend != "vibey":
            return self._exact_turn(backend, token)
        turn_id = self._named_route_turn(backend, token, request_metadata)
        if turn_id is None:
            return None
        trace = self._traces.get(turn_id)
        if trace is not None and (trace.ambiguous or trace.admission_closed):
            return None
        return turn_id, self._credential(backend, token).scope_key

    def _named_route_turn(
        self,
        backend: str,
        token: str,
        request_metadata: Mapping[str, str] | None,
    ) -> str | None:
        """The turn a request names, if it holds the request credential's route; call under `_lock`."""

        credential = self._credential(backend, token)
        scope = self._scopes.get(credential.scope_key) if credential is not None else None
        turn_id = request_metadata.get("avibe_turn_id") if isinstance(request_metadata, Mapping) else None
        if (
            scope is None
            or credential.route is None
            or not isinstance(turn_id, str)
            or not self._explicit_turn_owns_route(scope, turn_id=turn_id, route=credential.route)
        ):
            return None
        return turn_id

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
            self._credentials.pop(scope.token, None)
            for route_token in scope.route_tokens.values():
                self._credentials.pop(route_token, None)
            for turn_id in scope.active_turns:
                trace = self._traces.get(turn_id)
                terminal_is_exact = (
                    trace is not None
                    and (
                        scope.request_scoped or scope.active_turns == {turn_id}
                    )
                    and (
                        turn_id == normalized_terminal_turn_id
                        or trace.admission_closed
                    )
                    and not trace.ambiguous
                    and not scope.untracked_use
                    and turn_id not in scope.ambiguous_turns
                    and bool(trace.failed_attempts or trace.terminal_error or (trace.admission_closed and trace.served))
                )
                if terminal_is_exact:
                    turn_scopes = self._turn_scopes.get(turn_id)
                    if turn_scopes is not None:
                        turn_scopes.discard(key)
                elif trace is not None:
                    trace.ambiguous = True

    def _exact_turn(self, backend: str, token: str) -> tuple[str, ScopeKey] | None:
        credential = self._credential(backend, token)
        if credential is None:
            return None
        key = credential.scope_key
        scope = self._scopes.get(key)
        if scope is None or scope.request_scoped:
            return None
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
        if trace is not None and (trace.ambiguous or trace.admission_closed):
            return None
        return turn_id, key

    def _trusted_turn(
        self,
        *,
        backend: str,
        token: str,
        turn_id: str,
    ) -> tuple[str, ScopeKey] | None:
        """Resolve a dispatcher-owned turn without HTTP turn inference."""

        credential = self._credential(backend, token)
        if credential is None:
            return None
        key = credential.scope_key
        scope = self._scopes.get(key)
        if scope is None:
            return None
        if scope.request_scoped:
            if turn_id not in scope.active_turns or turn_id in scope.ambiguous_turns:
                return None
            trace = self._traces.get(turn_id)
            if trace is not None and (trace.ambiguous or trace.admission_closed):
                return None
            return turn_id, key
        exact = self._exact_turn(backend, token)
        return exact if exact is not None and exact[0] == turn_id else None

    def begin_gateway_request(
        self,
        *,
        backend: str,
        token: str,
        requested_model_id: str,
        request_metadata: Mapping[str, str] | None = None,
    ) -> Optional[str]:
        with self._lock:
            exact = self._request_turn(backend, token, request_metadata)
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
                trace.gateway_request_model_id is not None
                and requested_model_id not in {
                    trace.gateway_request_model_id,
                    trace.gateway_model_id,
                }
            ):
                trace.ambiguous = True
                self._scopes[key].ambiguous_turns.add(turn_id)
                return None
            return turn_id

    def _register_prepared_gateway_route(
        self,
        *,
        scope: ProcessScope,
        turn_id: str,
        route: PreparedGatewayRoute,
    ) -> bool:
        existing = scope.prepared_routes.get(turn_id)
        if existing is not None and existing != route:
            scope.prepared_routes.pop(turn_id, None)
            scope.routing_conflicts.add(turn_id)
            return False
        if turn_id in scope.routing_conflicts:
            return False
        scope.prepared_routes[turn_id] = route
        return True

    def _record_prepared_gateway_trace(
        self,
        *,
        key: ScopeKey,
        scope: ProcessScope,
        turn_id: str,
        route: PreparedGatewayRoute,
        hosted_tools: TurnHostedTools | None = None,
    ) -> bool:
        trace = self._traces.setdefault(
            turn_id,
            TurnTrace(
                turn_id=turn_id,
                agent=key[0],
                requested_model_id=route.requested_model_id,
                scope_key=key,
            ),
        )
        if (
            trace.requested_model_id != route.requested_model_id
            or (
                trace.gateway_source_id is not None
                and trace.gateway_source_id != route.source_id
            )
            or (
                trace.gateway_request_model_id is not None
                and trace.gateway_request_model_id
                != route.gateway_request_model_id
            )
            or (
                trace.gateway_model_id is not None
                and trace.gateway_model_id != route.resolved_model_id
            )
        ):
            trace.ambiguous = True
            scope.ambiguous_turns.add(turn_id)
            return False
        trace.gateway_source_id = route.source_id
        trace.gateway_request_model_id = route.gateway_request_model_id
        trace.gateway_model_id = route.resolved_model_id
        if trace.hosted_tools is None:
            trace.hosted_tools = hosted_tools
        return True

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
        gateway_request_model_id: str | None = None,
        primary_origin: HopOrigin | None = None,
        primary_identity: str | None = None,
        hosted_tools: TurnHostedTools | None = None,
    ) -> str:
        """Register a route and return its legacy credential or explicit handle.

        ``hosted_tools`` is what a Vibey turn's first resolution froze; the
        turn keeps the first it is given.
        """

        with self._lock:
            credential = self._credential(backend, token)
            if credential is None:
                return token
            key = credential.scope_key
            scope = self._scopes.get(key)
            if scope is None:
                return token
            normalized_turn_id = str(turn_id or "").strip()
            request_model_id = gateway_request_model_id or resolved_model_id
            prepared = PreparedGatewayRoute(
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                source_id=source_id,
                gateway_request_model_id=request_model_id,
                primary_origin=primary_origin,
                primary_identity=(
                    primary_identity
                    if isinstance(primary_identity, str)
                    else primary_origin.identity if primary_origin is not None else ""
                ),
            )

            if scope.request_scoped:
                launch_token = self._route_credential(key, scope, prepared)
                if (
                    normalized_turn_id
                    and normalized_turn_id in scope.active_turns
                ):
                    self._register_prepared_gateway_route(
                        scope=scope,
                        turn_id=normalized_turn_id,
                        route=prepared,
                    )
                    self._record_prepared_gateway_trace(
                        key=key,
                        scope=scope,
                        turn_id=normalized_turn_id,
                        route=prepared,
                        hosted_tools=hosted_tools,
                    )
                return launch_token

            if normalized_turn_id:
                if normalized_turn_id not in scope.active_turns:
                    return token
                route_turn_id = normalized_turn_id
            else:
                exact = self._exact_turn(backend, token)
                if exact is None:
                    if backend == "vibey":
                        # A consumer launch without a tracked turn still needs
                        # its alias and primary origin bound to this credential.
                        return self._route_credential(key, scope, prepared)
                    return token
                route_turn_id = exact[0]
            existing = scope.prepared_routes.get(route_turn_id)
            if backend == "vibey" and existing is not None and existing != prepared:
                trace = self._readable_trace(route_turn_id, backend)
                if (
                    trace is None
                    or trace.requested_model_id != requested_model_id
                    or trace.gateway_requests
                    or trace.pending_attempts
                    or trace.recovery_requests
                ):
                    raise PreparedGatewayRouteConflict("Vibey route is still owned or its alias changed")
                # Keep the turn's attempt history and outcome. Only the launch
                # snapshot changes; old credentials can route but cannot claim
                # this newly prepared identity.
                scope.prepared_routes.pop(route_turn_id)
                trace.gateway_source_id = None
                trace.gateway_request_model_id = None
                trace.gateway_model_id = None
            launch_token = self._route_credential(key, scope, prepared)
            self._register_prepared_gateway_route(
                scope=scope,
                turn_id=route_turn_id,
                route=prepared,
            )
            exact = self._exact_turn(backend, token)
            if exact is None or exact[0] != route_turn_id:
                return launch_token
            self._record_prepared_gateway_trace(
                key=key,
                scope=scope,
                turn_id=route_turn_id,
                route=prepared,
                hosted_tools=hosted_tools,
            )
            return launch_token

    def gateway_request_metadata(
        self,
        *,
        backend: str,
        token: str,
        turn_id: Optional[str],
        route: PreparedGatewayRoute | None = None,
    ) -> dict[str, str]:
        """Return a registered route, with an empty turn for untracked callers."""

        normalized_turn_id = str(turn_id or "").strip()
        with self._lock:
            credential = self._credential(backend, token)
            if credential is None:
                return {}
            scope = self._scopes.get(credential.scope_key)
            if (
                scope is None
                or not scope.request_scoped
            ):
                return {}
            if normalized_turn_id:
                if (
                    normalized_turn_id not in scope.active_turns
                    or normalized_turn_id in scope.routing_conflicts
                ):
                    return {}
                prepared = scope.prepared_routes.get(normalized_turn_id)
                if route is not None and route != prepared:
                    return {}
                route = prepared
            if route is None:
                return {}
            route_id = scope.route_tokens.get(route)
            if route_id is None:
                return {}
            return {
                "avibe_route_id": route_id,
                "avibe_turn_id": normalized_turn_id,
            }

    def claim_gateway_request(
        self,
        *,
        backend: str,
        token: str,
        prepared_turn_id: Optional[str],
        request_id: str = TURN_REQUEST,
        gateway_model_id: str,
        request_metadata: Mapping[str, str] | None = None,
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
                request_metadata=request_metadata,
            )
            if prepared_turn_id is None:
                return GatewayRouting(caller_model_id, None)
            credential = self._credential(backend, token)
            scope = (
                self._scopes.get(credential.scope_key)
                if credential is not None
                else None
            )
            if scope is not None and scope.request_scoped:
                trace = self._traces.get(prepared_turn_id)
                if (
                    claimed
                    and trace is not None
                    and not trace.ambiguous
                    and request_id in trace.pending_attempts
                ):
                    # Admission may close after this request opened. Its
                    # ownership remains valid through the bounded drain.
                    return GatewayRouting(caller_model_id, prepared_turn_id)
                self.clear_prepared_attempt(
                    prepared_turn_id,
                    request_id=request_id,
                )
                return GatewayRouting(caller_model_id, None)
            if claimed:
                trace = self._traces.get(prepared_turn_id)
                if (
                    trace is not None
                    and trace.admission_closed
                    and not trace.ambiguous
                    and request_id in trace.pending_attempts
                    and gateway_model_id in {trace.gateway_request_model_id, trace.gateway_model_id}
                ):
                    # This request acquired its identity before the FSM closed
                    # admission; completing its body parse is not a new claim.
                    return GatewayRouting(caller_model_id, prepared_turn_id)
                owner = self.begin_gateway_request(
                    backend=backend,
                    token=token,
                    requested_model_id=gateway_model_id,
                    request_metadata=request_metadata,
                )
                if owner != prepared_turn_id:
                    owner = None
                return GatewayRouting(caller_model_id, owner)
            # Routing was answered from this request's own credential, so no
            # live turn owns it and its own turn has already settled — there
            # is nothing left to attribute it to. The attempt armed for the
            # live turn on the way in therefore has to be given back: left
            # behind, it settles that turn as having canceled or interrupted a
            # Hub attempt it never made. It cannot be armed later instead,
            # because `fail` runs before this call and needs a turn to fail.
            self.clear_prepared_attempt(prepared_turn_id, request_id=request_id)
            return GatewayRouting(caller_model_id, None)

    def _route_gateway_model(
        self,
        *,
        backend: str,
        token: str,
        gateway_model_id: str,
        request_metadata: Mapping[str, str] | None = None,
    ) -> tuple[Optional[str], bool]:
        """Resolve where this gateway request goes; call under `_lock`.

        Routing is a property of the credential, not of the turn windows that
        happen to be open. The credential was minted for one route when the
        process was launched, so it answers for that route for as long as the
        process lives — through CLI tool loops, agent-initiated turns, and
        transport retries, none of which arrive inside a dispatched-turn
        window.

        Returns the caller model to route by, and whether a live turn claims
        it and may therefore be credited with the request.
        """

        credential = self._credential(backend, token)
        if credential is None:
            return None, False
        scope = self._scopes.get(credential.scope_key)
        if scope is None:
            return None, False
        if scope.request_scoped:
            explicit = self._explicit_route(
                backend=backend,
                token=token,
                request_metadata=request_metadata,
            )
            if explicit is None:
                return None, False
            _key, _scope, route, _route_id = explicit
            # An explicit handle proves which process holds it and which turn
            # it was minted for. It deliberately does not fix the model. A
            # dispatched turn names another of Avibe's own models as a matter
            # of course, and Codex always does at a model change: it
            # re-serialises the thread under the OUTGOING model before the
            # first turn on the new one, so that hop arrives on the new turn's
            # handle carrying the old id. Refusing it here strands the thread
            # for good — the re-serialisation never lands, so every later turn
            # repeats it and fails the same way. Whether Avibe configured the
            # named model is resolution's question, not routing's: the caller
            # model below reaches `effective_model_route`, which serves only
            # ids already in this agent's menu and returns an empty route for
            # anything else. Answering it a second time here would duplicate
            # that authority and report an unconfigured model as an
            # incompatible request.
            if gateway_model_id == route.gateway_request_model_id:
                # The one id that proves a request is on its own route: what
                # Avibe told this launch to send. The route's upstream target
                # proves nothing, because menu ids and upstream ids are
                # separate namespaces that may be spelled the same — reading
                # that spelling as this route's would send the outgoing
                # model's re-serialisation down the new route, to another
                # source under another model. Where no distinct id was minted
                # the two coincide, and this one comparison covers both.
                caller_model_id = route.requested_model_id
            else:
                caller_model_id = gateway_model_id
            identity = self._request_identity(request_metadata)
            assert identity is not None
            _route_id, turn_id = identity
            if turn_id in scope.active_turns:
                if not self._explicit_turn_owns_route(
                    scope,
                    turn_id=turn_id,
                    route=route,
                ):
                    return None, False
                return caller_model_id, True
            # Completed turn IDs are intentionally not retained. A valid
            # process-owned route handle can route a late continuation, but it
            # cannot claim a newer active turn.
            return caller_model_id, False
        route = credential.route
        if route is None:
            # No route was known when this credential was minted: the shared
            # OpenCode server, and any launch Avibe did not map. There the
            # wire model id really is the route key, because one credential
            # serves every model the process may name.
            return (gateway_model_id if scope.untracked_use else None), False
        if gateway_model_id not in {
            route.gateway_request_model_id,
            route.resolved_model_id,
        }:
            # This credential was minted for one route, and the route knows
            # both model ids a request on it may legitimately name: the id the
            # launch was told to send, and the resolved upstream id a backend
            # started before that became explicit still sends. A request for
            # anything else belongs to no route the process was launched on —
            # a CLI naming its own model, most often — and answering it from
            # some other route would be exactly the aliasing this credential
            # exists to prevent. Accepting both ids cannot alias here: the
            # credential already named the route, so there is no second route
            # for the legacy id to be confused with.
            return None, False
        # A live turn claims this request when it prepared this same route.
        # The whole route must match, not just the upstream model: two routes
        # can resolve to one upstream model id, and crediting a turn for a
        # request that belongs to the other one is how a turn ends up
        # ambiguous over a model it never asked for.
        claimed = (
            self._named_route_turn(backend, token, request_metadata) is not None
            if backend == "vibey"
            else any(
                prepared == route and turn_id in scope.active_turns
                for turn_id, prepared in scope.prepared_routes.items()
            )
        )
        return route.requested_model_id, claimed

    def gateway_terminalizer(
        self,
        *,
        backend: str,
        token: str,
        request_metadata: Mapping[str, str] | None = None,
    ) -> GatewayTurnTerminalizer:
        return GatewayTurnTerminalizer(
            self,
            backend=backend,
            token=token,
            request_metadata=request_metadata,
        )

    def _open_prepared_gateway_turn(
        self,
        *,
        backend: str,
        token: str,
        request_id: str = TURN_REQUEST,
        request_metadata: Mapping[str, str] | None = None,
    ) -> Optional[str]:
        """Arm the launch identity for one gateway request.

        The identity is filed under this request, so arming it can neither
        displace nor be displaced by a concurrent request on the same turn.
        Arming has to happen here, before any model id is read, because a
        request can fail on its parameters before it ever names a model and
        that failure still needs a turn to land on.
        """

        with self._lock:
            credential = self._credential(backend, token)
            scope = (
                self._scopes.get(credential.scope_key)
                if credential is not None
                else None
            )
            if scope is not None and scope.request_scoped:
                explicit = self._explicit_route(
                    backend=backend,
                    token=token,
                    request_metadata=request_metadata,
                )
                if explicit is None:
                    return None
                _key, scope, route, _route_id = explicit
                identity = self._request_identity(request_metadata)
                assert identity is not None
                _route_id, turn_id = identity
                if not self._explicit_turn_owns_route(
                    scope,
                    turn_id=turn_id,
                    route=route,
                ):
                    return None
                trace = self._traces.get(turn_id)
                if (
                    trace is None
                    or trace.ambiguous
                    or trace.admission_closed
                    or trace.gateway_source_id is None
                    or trace.gateway_model_id is None
                ):
                    return None
                trace.pending_attempts[request_id] = AttemptIdentity(
                    source_id=trace.gateway_source_id,
                    resolved_model_id=trace.gateway_model_id,
                    channel="hub",
                )
                return turn_id

            exact = self._request_turn(backend, token, request_metadata)
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
            if backend == "vibey":
                trace.gateway_requests.add(request_id)
            trace.pending_attempts[request_id] = AttemptIdentity(
                source_id=trace.gateway_source_id,
                resolved_model_id=trace.gateway_model_id,
                channel="hub",
            )
            return turn_id

    def close_gateway_request(self, turn_id: str | None, *, request_id: str) -> None:
        """Release request lifetime independently of its last attempt outcome."""

        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None:
                trace.gateway_requests.discard(request_id)

    def clear_prepared_attempt(
        self,
        turn_id: Optional[str],
        *,
        request_id: str = TURN_REQUEST,
    ) -> None:
        """Remove one request's launch identity when it never invokes.

        Only this request's own entry is removed. A peer awaiting an upstream
        result keeps its identity, which is the whole point of filing attempts
        per request: `finish_attempt` reconstructs nothing from an empty
        entry, so taking a peer's would lose its provenance for good.
        """

        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.outcome_frozen:
                return
            identity = trace.pending_attempts.get(request_id)
            if identity is not None and identity.channel == "hub":
                trace.pending_attempts.pop(request_id, None)

    def _terminalize_gateway_exit(
        self,
        turn_id: Optional[str],
        *,
        request_id: str = TURN_REQUEST,
        reason: Literal[
            "invalid_parameter",
            "protocol_error",
            "engine_down",
        ] = "protocol_error",
        stream_started: bool,
        force: bool = False,
        local_error_detail: str | None = None,
        response_correlation_ids: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.ambiguous or trace.outcome_frozen:
                return
            if not force and (
                trace.served is not None
                or trace.terminal_error is not None
                or trace.model_supply_state is not None
                or (
                    trace.pending_attempts.get(request_id) is None
                    and bool(trace.failed_attempts)
                )
            ):
                return
            if reason == "engine_down":
                # The engine, not one request, is what went down. Preserve
                # correlation facts when a later forced finalization repeats
                # this same terminalization without another response.
                previous_ids = ()
                if isinstance(trace.terminal_error, dict) and trace.terminal_error.get("reason") == reason:
                    previous = trace.terminal_error.get("response_correlation_ids")
                    if isinstance(previous, list):
                        previous_ids = tuple(
                            (item.get("header"), item.get("value"))
                            for item in previous
                            if isinstance(item, Mapping)
                        )
                correlation_payload = _response_correlation_payload(response_correlation_ids)
                if not correlation_payload:
                    correlation_payload = _response_correlation_payload(previous_ids)
                trace.pending_attempts.clear()
                trace.served = None
                trace.model_supply_state = None
                trace.blockers = []
                trace.terminal_error = {
                    "source_id": None,
                    "configured_model_id": None,
                    "channel": None,
                    "reason": reason,
                    "stream_started": stream_started,
                    **({"local_error_detail": local_error_detail} if local_error_detail else {}),
                    **correlation_payload,
                }
                return
            if self._peer_request_open(trace, request_id):
                trace.pending_attempts.pop(request_id, None)
                return
            identity = trace.pending_attempts.get(request_id)
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
            trace.pending_attempts.pop(request_id, None)
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
        if backend == "vibey":
            raise ValueError("vibey attempts require the hub channel")
        token = self.credentials(backend, process_scope, turn_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        with self._lock:
            exact = self._trusted_turn(
                backend=backend,
                token=token,
                turn_id=normalized_turn_id,
            )
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
            # A native turn is one request by construction.
            trace.pending_attempts[TURN_REQUEST] = AttemptIdentity(
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
            exact = self._trusted_turn(
                backend=backend,
                token=token,
                turn_id=normalized_turn_id,
            )
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
        *,
        request_id: str = TURN_REQUEST,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is not None and not trace.outcome_frozen:
                # Only this request found nothing to call.
                if self._peer_request_open(trace, request_id):
                    trace.pending_attempts.pop(request_id, None)
                    return
                trace.pending_attempts.pop(request_id, None)
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
            if trace is not None and not trace.outcome_frozen:
                trace.terminal_outcome = turn_outcome

    def commit_gateway_exit(
        self,
        turn_id: Optional[str],
        *,
        request_id: str,
        stream_started: bool,
        turn_outcome: TurnOutcomeProjectionInput | None = None,
        has_outcome: bool = False,
        no_candidate: tuple[SupplyState, tuple[ExactHopBlocker, ...]] | None = None,
        fail_reason: Literal["invalid_parameter", "protocol_error"] | None = None,
    ) -> None:
        """Commit one gateway request's whole exit under one lock.

        Beside an open peer the exit drops only this request's identity, so
        one exit is either wholly this request's or wholly the turn's.
        """

        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None:
                return
            if self._peer_request_open(trace, request_id):
                if not trace.outcome_frozen:
                    trace.pending_attempts.pop(request_id, None)
                return
            if no_candidate is not None:
                self.mark_gateway_no_candidate(turn_id, no_candidate[0], no_candidate[1], request_id=request_id)
            if fail_reason is not None:
                self._terminalize_gateway_exit(
                    turn_id, request_id=request_id, reason=fail_reason,
                    stream_started=stream_started, force=True,
                )
            if has_outcome:
                self.record_turn_outcome(turn_id, turn_outcome)

    def begin_attempt(
        self,
        turn_id: Optional[str],
        *,
        source_id: str,
        resolved_model_id: str,
        channel: SupplyChannel,
        via_mapping: bool,
        stripped_reasoning_efforts: tuple[str, ...] = (),
        declared_reasoning_efforts: tuple[str, ...] = (),
        request_id: str = TURN_REQUEST,
        origin: HopOrigin | None = None,
    ) -> None:
        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.outcome_frozen:
                return
            if trace.agent == "vibey" and channel != "hub":
                raise ValueError("vibey attempts require the hub channel")
            # A newly admitted attempt supersedes earlier request/supply
            # terminal facts, not completed fallback history. Otherwise an
            # interrupted successor would be blamed on the preceding request.
            trace.model_supply_state = None
            trace.blockers = []
            trace.terminal_error = None
            trace.terminal_outcome = None
            trace.pending_attempts[request_id] = AttemptIdentity(
                source_id=source_id,
                resolved_model_id=resolved_model_id,
                channel=channel,
                stripped_reasoning_efforts=stripped_reasoning_efforts,
                declared_reasoning_efforts=declared_reasoning_efforts,
                origin=origin,
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
            identity = (
                trace.pending_attempts.get(TURN_REQUEST)
                if trace is not None
                else None
            )
            if trace is None or trace.outcome_frozen or identity is None or identity.channel != "native_cli":
                return
            trace.pending_attempts.pop(TURN_REQUEST, None)
            trace.served = None
            trace.terminal_error = None
            trace.failed_attempts.append(
                {**identity.payload(), "reason": reason}
            )

    @staticmethod
    def _peer_request_open(trace: TurnTrace, request_id: str) -> bool:
        """Whether another request of this turn is still open.

        Every gateway request arms an identity on arrival, so this covers a
        peer that is waiting as well as one awaiting its upstream result. A
        request ending beside it records only its own exit: the turn settles
        on whichever request ends last.
        """

        return any(key != request_id for key in trace.pending_attempts)

    def fail_hub_attempt(
        self,
        turn_id: Optional[str],
        *,
        error_kind: str | None = None,
        diagnostic: str | None = None,
    ) -> None:
        """Record the backend's terminal failure of a turn the gateway did not end.

        The record holds only what the backend reported: its typed kind and its
        sanitized diagnostic. The backend cannot see the failing request's
        phase, so ``stream_started`` stays unknown.
        """

        normalized = str(turn_id or "").strip()
        if not normalized:
            return
        # Native backends may include credentialed URLs or Basic auth in their
        # transport diagnostic. Redact those before the shared display bound;
        # redacting only upstream-style labeled keys is insufficient here.
        detail = bounded_diagnostic_detail(redact_provider_text(diagnostic or ""))
        with self._lock:
            trace = self._traces.get(normalized)
            if trace is None or trace.outcome_frozen:
                return
            if self.terminal_projection(normalized, backend=trace.agent) is not None:
                # The native failure is reporting the gateway's completed
                # failure, not rejecting content that the gateway served.
                # Protect that acknowledged terminal while retaining its copy
                # for callers that read it after reporting backend failure.
                trace.outcome_frozen = "backend_failure"
                return
            if trace.terminal_error is not None and not trace.pending_attempts:
                # The same for a completed failure that has no copy. A request
                # still open after it would supersede it, but none is.
                trace.outcome_frozen = "backend_failure"
                return
            identity = trace.pending_attempt
            payload = (
                identity.payload()
                if identity is not None and identity.channel == "hub"
                else trace.served
            )
            if payload is not None:
                # A backend_error describes the backend's own report, not the
                # earlier gateway response that the backend rejected. The
                # response-only correlation facts are therefore not valid on
                # this terminal variant and must not cross its schema boundary.
                payload = {
                    key: value for key, value in payload.items()
                    if key != "response_correlation_ids"
                }
            if payload is None or payload.get("channel") != "hub":
                return
            # The backend rejected the turn's result, not one request's.
            # A callback without an attributed payload must not freeze out
            # gateway evidence that has yet to arrive.
            trace.outcome_frozen = "backend_failure"
            trace.pending_attempts.clear()
            trace.served = None
            trace.terminal_error = {
                **payload,
                "reason": "backend_error",
                "stream_started": None,
                **(
                    {"error_kind": error_kind}
                    if isinstance(error_kind, str) and _BACKEND_ERROR_KIND.fullmatch(error_kind)
                    else {}
                ),
                **({"backend_error_detail": detail} if detail else {}),
            }

    def finish_attempt(
        self,
        turn_id: Optional[str],
        *,
        outcome: RawCallOutcome,
        decision: ResolutionDecision,
        request_id: str = TURN_REQUEST,
    ) -> None:
        if turn_id is None:
            return
        if decision.error_code == "engine_down":
            self._terminalize_gateway_exit(
                turn_id,
                request_id=request_id,
                reason="engine_down",
                stream_started=outcome.stream_started,
                local_error_detail=format_os_errno(outcome.os_errno),
                response_correlation_ids=outcome.response_correlation_ids,
                force=True,
            )
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            identity = (
                trace.pending_attempts.get(request_id) if trace is not None else None
            )
            if trace is None or trace.outcome_frozen or identity is None:
                return
            trace.pending_attempts.pop(request_id, None)
            if decision.action == "return":
                trace.served = {
                    **identity.payload(),
                    **_response_correlation_payload(outcome.response_correlation_ids),
                }
                trace.terminal_error = None
                return
            # The engine client already redacted and bounded this text.
            detail = {
                "upstream_detail": outcome.upstream_detail
            } if outcome.upstream_detail else {}
            detail.update(_response_correlation_payload(outcome.response_correlation_ids))
            logger.warning(
                "Model Hub attempt failed turn=%s source=%s model=%s http_status=%s reason=%s "
                "stream_started=%s detail=%s",
                turn_id, identity.source_id, identity.resolved_model_id, outcome.http_status,
                decision.reason or decision.error_code, outcome.stream_started, outcome.upstream_detail,
            )
            if decision.action == "fallback" and decision.reason is not None:
                trace.failed_attempts.append(
                    {
                        **identity.payload(), "reason": decision.reason,
                        **({"http_status": outcome.http_status} if type(outcome.http_status) is int and 100 <= outcome.http_status <= 599 else {}),
                        **detail,
                    }
                )
                return
            if decision.action == "surface":
                observed_codes = machine_error_codes(outcome)
                diagnostic_code = (
                    "model_not_found"
                    if decision.error_code == "upstream_request_invalid" and "model_not_found" in observed_codes
                    else next((code for code in observed_codes if code in UPSTREAM_MACHINE_ERROR_CODES), None)
                )
                trace.terminal_error = {
                    **identity.payload(),
                    "reason": _terminal_reason(decision),
                    "stream_started": outcome.stream_started,
                    "http_status": (
                        outcome.http_status
                        if type(outcome.http_status) is int and 100 <= outcome.http_status <= 599
                        else None
                    ),
                    "upstream_error_code": diagnostic_code,
                    **detail,
                }
                trace.terminal_error_by = (request_id, trace.terminal_error)

    def withdraw_terminal_error(self, turn_id: Optional[str], *, request_id: str) -> None:
        """Drop the terminal a handed-back request surfaced; it did not end the turn."""

        if turn_id is None:
            return
        with self._lock:
            trace = self._traces.get(turn_id)
            if trace is None or trace.outcome_frozen or trace.terminal_error_by is None:
                return
            owner, value = trace.terminal_error_by
            if owner == request_id and trace.terminal_error is value:
                trace.terminal_error = None
                trace.terminal_error_by = None

    def close_turn_admission(self, turn_id: str, *, settled_by: Optional[str]) -> None:
        """Latch the FSM boundary without discarding already-owned requests."""

        with self._lock:
            trace = self._traces.get(str(turn_id or "").strip())
            if trace is None or trace.admission_closed:
                return
            trace.admission_closed = True
            had_recovery = bool(trace.recovery_requests)
            trace.recovery_requests.clear()
            if settled_by in _USER_CANCELLATIONS:
                # Preserve the exact facts present at Stop. Teardown may expose
                # a later producer success, but cannot change who ended the turn.
                trace.outcome_frozen = "user_cancellation"
        if had_recovery:
            self._notify_recovery_changed(turn_id)

    def settle(self, turn_id: str, *, settled_by: Optional[str], ts: Optional[str] = None) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        self.close_turn_admission(normalized_turn_id, settled_by=settled_by)
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
            if settled_by in _USER_CANCELLATIONS and not terminal_history_committed:
                outcome = "canceled"
                canceled_attempt = (
                    trace.pending_attempt.payload()
                    if trace.pending_attempt is not None
                    else None
                )
                served = None
                terminal_error = None
            elif settled_by in _USER_CANCELLATIONS:
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
                    "contract_version": 23,
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
