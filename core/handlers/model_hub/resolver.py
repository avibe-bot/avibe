"""Pure Model Hub turn resolution shared by runtime and API projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from config.v2_config import ModelHubConfig, ModelHubSourceConfig

from .identifiers import opencode_provider_id, parse_opencode_model_id


BackendName = Literal["claude", "codex", "opencode"]
ResolutionChannel = Literal["direct", "native_cli", "hub", "unavailable"]


@dataclass(frozen=True)
class ModelHubTurnResolution:
    backend: BackendName
    channel: ResolutionChannel
    requested_model: str
    target_model: str
    source: ModelHubSourceConfig | None
    matching_sources: tuple[ModelHubSourceConfig, ...] = ()
    candidates: tuple[ModelHubSourceConfig, ...] = ()
    provider: str | None = None
    mapping_applied: bool = False
    recoverable_source_ids: tuple[str, ...] = ()


def allowed_origins(source: ModelHubSourceConfig) -> tuple[str, ...]:
    if source.kind == "api_key":
        return ()
    if source.vendor == "anthropic":
        return ("claude",)
    if source.vendor == "openai":
        return ("codex",)
    return ()


def source_eligible_for_backend(source: ModelHubSourceConfig, backend: str) -> bool:
    if source.kind == "api_key":
        return source.supply_channel == "hub"
    return backend in allowed_origins(source)


def source_retry_ready(source: ModelHubSourceConfig, now: datetime | None) -> bool:
    if source.state.status != "cooldown" or now is None:
        return False
    try:
        retry_at = datetime.fromisoformat((source.state.retry_at or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return retry_at <= now


def resolve_model_hub_turn(
    config: ModelHubConfig,
    backend: BackendName,
    requested_model: str,
    *,
    now: datetime | None = None,
    unavailable_source_ids: frozenset[str] = frozenset(),
    supply_channel: Literal["hub"] | None = None,
) -> ModelHubTurnResolution:
    """Resolve one turn without starting runtimes, reading credentials, or mutating state.

    ``now`` makes cooldown recovery deterministic. ``unavailable_source_ids`` is
    caller-supplied runtime viability discovered before this pure decision.
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

    mapping = next(
        (
            item
            for item in agent.mappings
            if item.enabled and item.builtin_id == requested_model
        ),
        None,
    )
    target_model = mapping.target_model_id if mapping is not None else requested_model
    mapping_applied = target_model != requested_model
    provider: str | None = None

    if backend == "opencode":
        if agent.menu is None or not agent.menu.checked:
            return ModelHubTurnResolution(
                backend=backend,
                channel="direct",
                requested_model=requested_model,
                target_model=requested_model,
                source=None,
            )
        try:
            provider, target_model = parse_opencode_model_id(target_model)
        except ValueError:
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=target_model,
                source=None,
                mapping_applied=mapping_applied,
            )
        if f"{provider}/{target_model}" not in agent.menu.checked:
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=target_model,
                source=None,
                provider=provider,
                mapping_applied=mapping_applied,
            )
    if not target_model:
        return ModelHubTurnResolution(
            backend=backend,
            channel=(
                "direct"
                if backend != "opencode" and mapping is None
                else "unavailable"
            ),
            requested_model=requested_model,
            target_model=target_model,
            source=None,
            provider=provider,
            mapping_applied=mapping_applied,
        )

    by_id = {source.id: source for source in config.sources}
    matching_sources = tuple(
        source
        for source in (by_id[source_id] for source_id in config.priority_order)
        if source_eligible_for_backend(source, backend)
        and (provider is None or opencode_provider_id(source.vendor) == provider)
        and any(model.id == target_model for model in source.models)
    )
    if backend != "opencode" and mapping is None and not matching_sources:
        return ModelHubTurnResolution(
            backend=backend,
            channel="direct",
            requested_model=requested_model,
            target_model=requested_model,
            source=None,
        )

    candidates: list[ModelHubSourceConfig] = []
    recoverable_source_ids: list[str] = []
    for source in matching_sources:
        if supply_channel is not None and source.supply_channel != supply_channel:
            continue
        if source.id in unavailable_source_ids or source.state.status == "error":
            continue
        if source.state.status == "cooldown":
            if not source_retry_ready(source, now):
                continue
            recoverable_source_ids.append(source.id)
        candidates.append(source)

    source = candidates[0] if candidates else None
    return ModelHubTurnResolution(
        backend=backend,
        channel=source.supply_channel if source is not None else "unavailable",
        requested_model=requested_model,
        target_model=target_model,
        source=source,
        matching_sources=matching_sources,
        candidates=tuple(candidates),
        provider=provider,
        mapping_applied=mapping_applied,
        recoverable_source_ids=tuple(recoverable_source_ids),
    )
