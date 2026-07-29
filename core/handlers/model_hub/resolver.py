"""Pure Model Hub turn resolution shared by runtime and API projections."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from config.v2_config import ModelHubConfig, ModelHubSourceConfig

from .identifiers import opencode_provider_id, parse_opencode_model_id


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
    provider: str | None = None
    mapping_applied: bool = False
    recoverable_source_ids: tuple[str, ...] = ()
    supply_status: SupplyStatus | None = None


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
    """Project the source state used by the next turn after retry recovery."""

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
) -> bool:
    if source.id in unavailable_source_ids:
        return False
    if source.state.status in {"active", "standby"}:
        return True
    return source_retry_ready(source, now)


def _supply_status(
    matching_sources: tuple[ModelHubSourceConfig, ...],
    candidates: tuple[ModelHubSourceConfig, ...],
    *,
    now: datetime | None,
    unavailable_source_ids: frozenset[str],
) -> SupplyStatus:
    if not matching_sources:
        return "interrupted"
    if candidates:
        all_runnable = all(
            source_runnable(
                source,
                now=now,
                unavailable_source_ids=unavailable_source_ids,
            )
            for source in matching_sources
        )
        return "ok" if candidates[0].id == matching_sources[0].id and all_runnable else "degraded"
    if all(
        source.id not in unavailable_source_ids
        and source.state.status == "cooldown"
        and not source_retry_ready(source, now)
        for source in matching_sources
    ):
        return "waiting"
    return "interrupted"


def normalize_opencode_requested_model(
    requested_model: str,
    checked_identifiers: tuple[str, ...],
) -> str | None:
    """Match a full or uniquely bare OpenCode model to its checked identifier."""

    candidate = str(requested_model or "").strip()
    if not candidate:
        return None
    if candidate in checked_identifiers:
        return candidate
    matches = [identifier for identifier in checked_identifiers if identifier.endswith(f"/{candidate}")]
    return matches[0] if len(matches) == 1 else None


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

    if backend == "opencode":
        checked = tuple(agent.menu.checked if agent.menu else ())
        if not checked:
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=requested_model,
                source=None,
                supply_status="interrupted",
            )
        if not requested_model:
            for identifier in checked:
                candidate = resolve_model_hub_turn(
                    config,
                    backend,
                    identifier,
                    now=now,
                    unavailable_source_ids=unavailable_source_ids,
                    supply_channel=supply_channel,
                )
                if candidate.source is not None:
                    return candidate
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=requested_model,
                source=None,
                supply_status="interrupted",
            )
        normalized = normalize_opencode_requested_model(requested_model, checked)
        if normalized is None:
            return ModelHubTurnResolution(
                backend=backend,
                channel="unavailable",
                requested_model=requested_model,
                target_model=requested_model,
                source=None,
                supply_status="interrupted",
            )
        requested_model = normalized

    mapping = (
        next(
            (item for item in agent.mappings if item.enabled and item.builtin_id == requested_model),
            None,
        )
        if agent.menu_kind == "fixed"
        else None
    )
    target_model = mapping.target_model_id if mapping is not None else requested_model
    mapping_applied = target_model != requested_model
    provider: str | None = None

    if backend == "opencode":
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
                supply_status="interrupted",
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
                supply_status="interrupted",
            )
    if not target_model:
        return ModelHubTurnResolution(
            backend=backend,
            channel="unavailable",
            requested_model=requested_model,
            target_model=target_model,
            source=None,
            provider=provider,
            mapping_applied=mapping_applied,
            supply_status="interrupted",
        )

    by_id = {source.id: source for source in config.sources}
    matching_sources = tuple(
        source
        for source in (by_id[source_id] for source_id in config.effective_source_order(backend))
        if source_eligible_for_backend(source, backend)
        and (provider is None or opencode_provider_id(source.vendor) == provider)
        and any(model.id == target_model for model in source.models)
    )

    candidates: list[ModelHubSourceConfig] = []
    recoverable_source_ids: list[str] = []
    for source in matching_sources:
        if supply_channel is not None and source.supply_channel != supply_channel:
            continue
        if source.state.status == "cooldown" and source_retry_ready(source, now):
            recoverable_source_ids.append(source.id)
        if not source_runnable(
            source,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
        ):
            continue
        candidates.append(source)

    source = candidates[0] if candidates else None
    candidate_tuple = tuple(candidates)
    return ModelHubTurnResolution(
        backend=backend,
        channel=source.supply_channel if source is not None else "unavailable",
        requested_model=requested_model,
        target_model=target_model,
        source=source,
        matching_sources=matching_sources,
        candidates=candidate_tuple,
        provider=provider,
        mapping_applied=mapping_applied,
        recoverable_source_ids=tuple(recoverable_source_ids),
        supply_status=_supply_status(
            matching_sources,
            candidate_tuple,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
        ),
    )
