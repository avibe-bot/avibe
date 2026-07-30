"""Pure Model Hub turn resolution shared by runtime and API projections."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from config.v2_config import ModelHubConfig, ModelHubSourceConfig

from .identifiers import opencode_provider_id, parse_opencode_model_id


BackendName = Literal["claude", "codex", "opencode"]
ResolutionChannel = Literal["direct", "native_cli", "hub", "unavailable"]
SupplyStatus = Literal["ok", "degraded", "waiting", "interrupted"]

_NATIVE_VENDOR_BY_BACKEND: dict[BackendName, str] = {
    "claude": "anthropic",
    "codex": "openai",
}
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
    provider: str | None = None
    mapping_applied: bool = False
    recoverable_source_ids: tuple[str, ...] = ()
    supply_status: SupplyStatus | None = None

    def model_for_source(self, source: ModelHubSourceConfig | str) -> str | None:
        source_id = source if isinstance(source, str) else source.id
        return next(
            (
                model_id
                for candidate_source_id, model_id in self.source_model_ids
                if candidate_source_id == source_id
            ),
            None,
        )


def _parsed_claude_model_id(
    model_id: str,
) -> tuple[str, tuple[int, ...], int | None] | None:
    match = _CLAUDE_MODEL_ID.fullmatch(model_id)
    if match is None:
        return None
    return (
        match.group("family"),
        tuple(int(part) for part in match.group("version").split("-")),
        int(match.group("date")) if match.group("date") else None,
    )


def _native_claude_alias(
    requested_model: str,
    source: ModelHubSourceConfig,
) -> str | None:
    family = _CLAUDE_FAMILY_ALIASES.get(requested_model)
    requested_version: tuple[int, ...] | None = None
    if family is None:
        parsed_request = _parsed_claude_model_id(requested_model)
        if parsed_request is None:
            return None
        family, requested_version, requested_date = parsed_request
        if requested_date is not None:
            return None

    matches: list[tuple[tuple[int, ...], int, str]] = []
    for model in source.models:
        if model.provenance != "discovered":
            continue
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


def _native_alias_for_source(
    backend: BackendName,
    requested_model: str,
    source: ModelHubSourceConfig,
) -> str | None:
    if source.vendor != _NATIVE_VENDOR_BY_BACKEND.get(backend):
        return None
    if backend == "claude":
        return _native_claude_alias(requested_model, source)
    return None


def effective_model_for_source(
    *,
    backend: BackendName,
    requested_model: str,
    target_model: str,
    source: ModelHubSourceConfig,
    explicit_mapping: bool,
) -> str | None:
    """Return the upstream id this source can actually supply for one menu id."""

    if not explicit_mapping:
        native_alias = _native_alias_for_source(
            backend,
            requested_model,
            source,
        )
        if native_alias is not None:
            return native_alias
    if any(model.id == target_model for model in source.models):
        return target_model
    return None


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
    source_model_ids: list[tuple[str, str]] = []
    matching_sources: list[ModelHubSourceConfig] = []
    for source in (
        by_id[source_id]
        for source_id in config.effective_source_order(backend)
    ):
        if not source_eligible_for_backend(source, backend) or (
            provider is not None and opencode_provider_id(source.vendor) != provider
        ):
            continue
        effective_model = effective_model_for_source(
            backend=backend,
            requested_model=requested_model,
            target_model=target_model,
            source=source,
            explicit_mapping=mapping is not None,
        )
        if effective_model is None:
            continue
        matching_sources.append(source)
        source_model_ids.append((source.id, effective_model))
    matching_source_tuple = tuple(matching_sources)

    candidates: list[ModelHubSourceConfig] = []
    recoverable_source_ids: list[str] = []
    for source in matching_source_tuple:
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
    target_source = source or (
        matching_source_tuple[0] if matching_source_tuple else None
    )
    effective_target = dict(source_model_ids).get(
        target_source.id if target_source is not None else "",
        target_model,
    )
    return ModelHubTurnResolution(
        backend=backend,
        channel=source.supply_channel if source is not None else "unavailable",
        requested_model=requested_model,
        target_model=effective_target,
        source=source,
        matching_sources=matching_source_tuple,
        candidates=candidate_tuple,
        source_model_ids=tuple(source_model_ids),
        provider=provider,
        mapping_applied=mapping_applied,
        recoverable_source_ids=tuple(recoverable_source_ids),
        supply_status=_supply_status(
            matching_source_tuple,
            candidate_tuple,
            now=now,
            unavailable_source_ids=unavailable_source_ids,
        ),
    )
