"""Pure Model Hub resolution over the persisted per-model route."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from config.v2_config import (
    MODEL_HUB_BACKENDS,
    ModelHubConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
)


BackendName = Literal["claude", "codex", "opencode"]
ResolutionChannel = Literal["direct", "native_cli", "hub", "unavailable"]
SupplyStatus = Literal["ok", "degraded", "waiting", "interrupted"]


@dataclass(frozen=True)
class ExactHopInspection:
    """Canonical identity and live eligibility for one persisted Route hop."""

    backend: BackendName
    menu_model: str
    source_id: str | None
    model_id: str | None
    source: ModelHubSourceConfig | None
    configuration_eligible: bool
    inventory_member: bool
    supply_eligible: bool
    runnable: bool
    structural_blocker: bool
    reason: str | None
    retry_at: str | None

    @property
    def identity(self) -> tuple[BackendName, str, str | None, str | None]:
        return self.backend, self.menu_model, self.source_id, self.model_id


@dataclass(frozen=True)
class ModelHubTurnResolution:
    backend: BackendName
    channel: ResolutionChannel
    requested_model: str
    target_model: str
    source: ModelHubSourceConfig | None
    matching_sources: tuple[ModelHubSourceConfig, ...] = ()
    candidates: tuple[ModelHubSourceConfig, ...] = ()
    candidate_hops: tuple[ExactHopInspection, ...] = ()
    projectable_hops: tuple[ExactHopInspection, ...] = ()
    source_model_ids: tuple[tuple[str, str], ...] = ()
    inspected_hops: tuple[ExactHopInspection, ...] = ()
    unsupported_source_ids: tuple[str, ...] = ()
    route_unconfigured: bool = False
    route_reason: str | None = None
    provider: str | None = None
    recoverable_source_ids: tuple[str, ...] = ()
    supply_status: SupplyStatus | None = None

    @property
    def structural_blocker_reason(self) -> str | None:
        if self.route_reason == "route_unconfigured":
            return self.route_reason
        return next(
            (
                inspection.reason
                for inspection in self.inspected_hops
                if inspection.structural_blocker
            ),
            None,
        )


def allowed_origins(source: ModelHubSourceConfig) -> tuple[str, ...]:
    if source.kind == "api_key":
        return ()
    if source.supply_channel == "hub":
        return tuple(MODEL_HUB_BACKENDS)
    if source.vendor == "anthropic":
        return ("claude",)
    if source.vendor == "openai":
        return ("codex",)
    return ()


def source_eligible_for_backend(source: ModelHubSourceConfig, backend: str) -> bool:
    return ModelHubConfig.source_eligible_for_backend(source, backend)


def source_retry_ready(source: ModelHubSourceConfig, now: datetime | None) -> bool:
    if source.state.status != "cooldown" or now is None:
        return False
    try:
        retry_at = datetime.fromisoformat((source.state.retry_at or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return retry_at <= now


def source_after_cooldown_recovery(
    source: ModelHubSourceConfig,
    now: datetime | None,
) -> ModelHubSourceConfig:
    if not source_retry_ready(source, now):
        return source
    return replace(
        source,
        state=replace(
            source.state,
            status=("active" if source.supply_channel == "native_cli" else "standby"),
            retry_at=None,
            detail_key=None,
        ),
    )


def source_runnable(
    source: ModelHubSourceConfig,
    *,
    now: datetime | None,
    unavailable_source_ids: frozenset[str] = frozenset(),
    model_supported: bool = True,
) -> bool:
    if not model_supported:
        return False
    if source.id in unavailable_source_ids:
        return False
    if source.state.status in {"active", "standby"}:
        return True
    return source_retry_ready(source, now)


_CLAUDE_FAMILY_ALIASES = {
    "opus": "opus",
    "opus[1m]": "opus",
    "sonnet": "sonnet",
    "sonnet[1m]": "sonnet",
    "haiku": "haiku",
}
_CLAUDE_MODEL_ID = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku|fable)-(?P<version>\d+(?:-\d+)*?)(?:-(?P<date>\d{8}))?$"
)


def _parsed_claude_model_id(model_id: str) -> tuple[str, tuple[int, ...], int | None] | None:
    match = _CLAUDE_MODEL_ID.fullmatch(model_id)
    if match is None:
        return None
    return (
        match.group("family"),
        tuple(int(part) for part in match.group("version").split("-")),
        int(match.group("date")) if match.group("date") else None,
    )


def matching_v1_model_id(
    *,
    backend: BackendName,
    requested_model: str,
    source: ModelHubSourceConfig,
    include_manual: bool = False,
) -> str | None:
    """Return one concrete observed model for the frozen add-time matching-v1."""

    observed_models = tuple(
        model
        for model in source.models
        if (include_manual or model.provenance == "discovered") and not model.retired
    )

    if (
        backend == "claude"
        and source.vendor == "anthropic"
        and source.supply_channel == "native_cli"
    ):
        family = _CLAUDE_FAMILY_ALIASES.get(requested_model)
        requested_version: tuple[int, ...] | None = None
        parsed_request = _parsed_claude_model_id(requested_model)
        if family is None and parsed_request is not None:
            family, requested_version, requested_date = parsed_request
            if requested_date is not None:
                return next(
                    (model.id for model in observed_models if model.id == requested_model),
                    None,
                )
        if family is not None:
            matches: list[tuple[tuple[int, ...], int, str]] = []
            for model in observed_models:
                parsed = _parsed_claude_model_id(model.id)
                if parsed is None:
                    continue
                candidate_family, candidate_version, candidate_date = parsed
                if candidate_family != family:
                    continue
                if requested_version is not None and candidate_version != requested_version:
                    continue
                matches.append((candidate_version, candidate_date or 0, model.id))
            return max(matches)[2] if matches else None

    return next(
        (model.id for model in observed_models if model.id == requested_model),
        None,
    )


def inspect_exact_hop(
    config: ModelHubConfig,
    backend: BackendName,
    menu_model: str,
    hop: ModelHubRouteHopConfig | None,
    *,
    now: datetime | None = None,
    unavailable_source_ids: frozenset[str] = frozenset(),
    supply_channel: Literal["hub"] | None = None,
) -> ExactHopInspection:
    """Inspect one stored hop without matching, substituting, or reordering it."""

    if hop is None:
        return ExactHopInspection(
            backend=backend,
            menu_model=menu_model,
            source_id=None,
            model_id=None,
            source=None,
            configuration_eligible=False,
            inventory_member=False,
            supply_eligible=False,
            runnable=False,
            structural_blocker=True,
            reason="route_unconfigured",
            retry_at=None,
        )

    source = next((item for item in config.sources if item.id == hop.source_id), None)
    if source is None:
        return ExactHopInspection(
            backend=backend,
            menu_model=menu_model,
            source_id=hop.source_id,
            model_id=hop.model_id,
            source=None,
            configuration_eligible=False,
            inventory_member=False,
            supply_eligible=False,
            runnable=False,
            structural_blocker=True,
            reason="source_missing",
            retry_at=None,
        )

    configuration_eligible = source_eligible_for_backend(source, backend)
    inventory_member = any(
        model.id == hop.model_id and not model.retired
        for model in source.models
    )
    channel_eligible = supply_channel is None or source.supply_channel == supply_channel
    supply_eligible = configuration_eligible and inventory_member and channel_eligible
    runnable = supply_eligible and source_runnable(
        source,
        now=now,
        unavailable_source_ids=unavailable_source_ids,
        model_supported=inventory_member,
    )
    reason: str | None = None
    structural_blocker = False
    if not inventory_member or not configuration_eligible:
        reason = "model_unsupported"
        structural_blocker = True
    elif source.id in unavailable_source_ids:
        reason = "native_cli_unavailable"
        structural_blocker = True
    elif source.state.status in {"needs_action", "error"}:
        reason = source.state.detail_key
    return ExactHopInspection(
        backend=backend,
        menu_model=menu_model,
        source_id=source.id,
        model_id=hop.model_id,
        source=source,
        configuration_eligible=configuration_eligible,
        inventory_member=inventory_member,
        supply_eligible=supply_eligible,
        runnable=runnable,
        structural_blocker=structural_blocker,
        reason=reason,
        retry_at=(
            source.state.retry_at
            if source.state.status == "cooldown"
            else None
        ),
    )


def _supply_status(
    inspections: tuple[ExactHopInspection, ...],
    *,
    now: datetime | None,
    unavailable_source_ids: frozenset[str],
) -> SupplyStatus:
    if not inspections:
        return "interrupted"
    if any(inspection.runnable for inspection in inspections):
        return "ok" if all(inspection.runnable for inspection in inspections) else "degraded"
    if all(
        inspection.source is not None
        and inspection.source.id not in unavailable_source_ids
        and inspection.supply_eligible
        and inspection.source.state.status == "cooldown"
        and not source_retry_ready(inspection.source, now)
        for inspection in inspections
    ):
        return "waiting"
    return "interrupted"


def resolve_model_hub_turn(
    config: ModelHubConfig,
    backend: BackendName,
    requested_model: str,
    *,
    now: datetime | None = None,
    unavailable_source_ids: frozenset[str] = frozenset(),
    supply_channel: Literal["hub"] | None = None,
) -> ModelHubTurnResolution:
    """Resolve a turn by walking the route persisted for ``requested_model``.

    This function deliberately does not inspect source inventory, vendor names,
    or source order to create a route. Those decisions belong to source-add and
    explicit chain-edit operations.
    """

    requested_model = str(requested_model or "").strip()
    agent = config.agents[backend]
    if agent.mode == "direct":
        return ModelHubTurnResolution(
            backend=backend,
            channel="direct",
            requested_model=requested_model,
            target_model=requested_model,
            source=None,
        )

    if backend == "opencode":
        if not requested_model:
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=requested_model,
                source=None,
                supply_status="interrupted",
            )

    route = agent.routes.get(requested_model)
    if route is None:
        return ModelHubTurnResolution(
            backend=backend,
            channel="unavailable",
            requested_model=requested_model,
            target_model=requested_model,
            source=None,
            supply_status="interrupted",
            route_unconfigured=True,
            route_reason="route_unconfigured",
        )

    inspected_hops = tuple(
        inspect_exact_hop(
            config,
            backend,
            requested_model,
            hop,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
            supply_channel=supply_channel,
        )
        for hop in route.hops
    )
    route_reason = (
        inspect_exact_hop(
            config,
            backend,
            requested_model,
            None,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
            supply_channel=supply_channel,
        ).reason
        if not route.hops
        else None
    )
    matching_inspections = tuple(
        inspection
        for inspection in inspected_hops
        if inspection.source is not None
    )
    matching_sources = tuple(
        inspection.source
        for inspection in matching_inspections
        if inspection.source is not None
    )
    source_model_ids = tuple(
        (inspection.source_id, inspection.model_id)
        for inspection in matching_inspections
        if inspection.source_id is not None and inspection.model_id is not None
    )
    unsupported_source_ids = frozenset(
        inspection.source_id
        for inspection in matching_inspections
        if inspection.reason == "model_unsupported"
        and inspection.source_id is not None
    )
    runnable_inspections = tuple(
        inspection
        for inspection in matching_inspections
        if inspection.runnable
    )
    candidates = tuple(
        inspection.source
        for inspection in runnable_inspections
        if inspection.source is not None
    )
    projectable_hops = tuple(
        inspection
        for inspection in matching_inspections
        if inspection.configuration_eligible
        and (
            supply_channel is None
            or (
                inspection.source is not None
                and inspection.source.supply_channel == supply_channel
            )
        )
    )
    recoverable = tuple(
        source.id
        for source in matching_sources
        if source.state.status == "cooldown" and source_retry_ready(source, now)
    )
    first_inspection = runnable_inspections[0] if runnable_inspections else None
    target_model = (
        first_inspection.model_id
        if first_inspection is not None
        else inspected_hops[0].model_id
        if inspected_hops
        else requested_model
    )
    channel: ResolutionChannel = "unavailable"
    first = first_inspection.source if first_inspection is not None else None
    if first is not None:
        channel = first.supply_channel
    return ModelHubTurnResolution(
        backend=backend,
        channel=channel,
        requested_model=requested_model,
        target_model=target_model or requested_model,
        source=first,
        matching_sources=matching_sources,
        candidates=candidates,
        candidate_hops=runnable_inspections,
        projectable_hops=projectable_hops,
        source_model_ids=source_model_ids,
        inspected_hops=inspected_hops,
        unsupported_source_ids=tuple(unsupported_source_ids),
        route_unconfigured=route_reason == "route_unconfigured",
        route_reason=route_reason,
        recoverable_source_ids=recoverable,
        supply_status=_supply_status(
            inspected_hops,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
        ),
    )
