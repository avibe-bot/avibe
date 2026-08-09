"""Pure Model Hub resolution over the persisted per-model route."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from config.v2_config import ModelHubConfig, ModelHubSourceConfig


BackendName = Literal["claude", "codex", "opencode"]
ResolutionChannel = Literal["direct", "native_cli", "hub", "unavailable"]
SupplyStatus = Literal["ok", "degraded", "waiting", "interrupted"]


@dataclass(frozen=True)
class ModelHubTurnResolution:
    backend: BackendName
    channel: ResolutionChannel
    requested_model: str
    target_model: str
    source: ModelHubSourceConfig | None
    matching_sources: tuple[ModelHubSourceConfig, ...] = ()
    candidates: tuple[ModelHubSourceConfig, ...] = ()
    source_model_ids: tuple[tuple[str, str], ...] = ()
    unsupported_source_ids: tuple[str, ...] = ()
    provider: str | None = None
    recoverable_source_ids: tuple[str, ...] = ()
    supply_status: SupplyStatus | None = None

    def model_for_source(self, source: ModelHubSourceConfig | str) -> str | None:
        source_id = source if isinstance(source, str) else source.id
        return next(
            (model_id for candidate_source_id, model_id in self.source_model_ids if candidate_source_id == source_id),
            None,
        )


def allowed_origins(source: ModelHubSourceConfig) -> tuple[str, ...]:
    if source.kind == "api_key":
        return ()
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


def normalize_opencode_requested_model(
    requested_model: str,
    checked_identifiers: tuple[str, ...],
) -> str | None:
    """Normalize a bare OpenCode selection to one exact checked menu id."""

    candidate = str(requested_model or "").strip()
    if not candidate:
        return None
    if candidate in checked_identifiers:
        return candidate
    matches = [identifier for identifier in checked_identifiers if identifier.endswith(f"/{candidate}")]
    return matches[0] if len(matches) == 1 else None


def _supply_status(
    matching_sources: tuple[ModelHubSourceConfig, ...],
    candidates: tuple[ModelHubSourceConfig, ...],
    *,
    now: datetime | None,
    unavailable_source_ids: frozenset[str],
    unsupported_source_ids: frozenset[str],
) -> SupplyStatus:
    if not matching_sources:
        return "interrupted"
    if candidates:
        return "ok" if candidates[0].id == matching_sources[0].id else "degraded"
    if all(
        source.id not in unavailable_source_ids
        and source.id not in unsupported_source_ids
        and source.state.status == "cooldown"
        and not source_retry_ready(source, now)
        for source in matching_sources
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
        checked = tuple(agent.menu.checked if agent.menu else ())
        if requested_model and requested_model not in checked:
            requested_model = ""
        elif not requested_model and checked:
            requested_model = checked[0]
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
        )

    by_id = {source.id: source for source in config.sources}
    hops = tuple(hop for hop in route.hops if hop.source_id in by_id)
    matching_sources = tuple(by_id[hop.source_id] for hop in hops)
    source_model_ids = tuple((hop.source_id, hop.model_id) for hop in hops)
    unsupported_source_ids = frozenset(
        source.id
        for source, hop in zip(matching_sources, hops)
        if not any(model.id == hop.model_id for model in source.models)
    )
    runnable_hops = tuple(
        (source, hop)
        for source, hop in zip(matching_sources, hops)
        if (supply_channel is None or source.supply_channel == supply_channel)
        and source_runnable(
            source,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
            model_supported=source.id not in unsupported_source_ids,
        )
    )
    candidates = tuple(source for source, _hop in runnable_hops)
    recoverable = tuple(
        source.id
        for source in matching_sources
        if source.state.status == "cooldown" and source_retry_ready(source, now)
    )
    first_pair = runnable_hops[0] if runnable_hops else None
    target_model = first_pair[1].model_id if first_pair is not None else (hops[0].model_id if hops else requested_model)
    channel: ResolutionChannel = "unavailable"
    first = first_pair[0] if first_pair is not None else None
    if first is not None:
        channel = first.supply_channel
    return ModelHubTurnResolution(
        backend=backend,
        channel=channel,
        requested_model=requested_model,
        target_model=target_model,
        source=first,
        matching_sources=matching_sources,
        candidates=candidates,
        source_model_ids=source_model_ids,
        unsupported_source_ids=tuple(unsupported_source_ids),
        recoverable_source_ids=recoverable,
        supply_status=_supply_status(
            matching_sources,
            candidates,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
            unsupported_source_ids=unsupported_source_ids,
        ),
    )
