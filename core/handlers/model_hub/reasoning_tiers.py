"""Single provenance ladder for Source-model reasoning effort declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from vibe.backend_model_catalog import (
    PROTOCOL_REASONING_EFFORT_DEFAULTS,
    bundled_catalog_reasoning_efforts_for_model,
)

ReasoningEffortsSource = Literal["upstream", "catalog", "user"] | None

_UPSTREAM_REASONING_PARAMETERS = frozenset({"reasoning", "reasoning_effort"})


@dataclass(frozen=True)
class ReasoningTierResolution:
    efforts: tuple[str, ...]
    source: ReasoningEffortsSource


def resolve_reasoning_tiers(
    *,
    protocol: str,
    model_id: str,
    supported_parameters: tuple[str, ...] | None = None,
    existing_efforts: Sequence[str] = (),
    existing_source: ReasoningEffortsSource = None,
) -> ReasoningTierResolution:
    """Apply upstream, bundled-catalog, then user provenance in that order."""

    if supported_parameters is not None and any(
        parameter.strip().lower() in _UPSTREAM_REASONING_PARAMETERS
        for parameter in supported_parameters
    ):
        defaults = PROTOCOL_REASONING_EFFORT_DEFAULTS.get(protocol)
        if defaults is not None:
            return ReasoningTierResolution(defaults, "upstream")

    catalog_efforts = bundled_catalog_reasoning_efforts_for_model(model_id)
    if catalog_efforts is not None:
        return ReasoningTierResolution(catalog_efforts, "catalog")

    if existing_source == "user" and existing_efforts:
        return ReasoningTierResolution(tuple(existing_efforts), "user")
    return ReasoningTierResolution((), None)
