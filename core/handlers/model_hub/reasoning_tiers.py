"""Single provenance ladder for Source-model reasoning effort declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from vibe.backend_model_catalog import (
    PROTOCOL_REASONING_EFFORT_DEFAULTS,
    bundled_catalog_client_presets_by_model,
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
    catalog_efforts_by_model: Mapping[str, tuple[str, ...]] | None = None,
    client_presets_by_model: Mapping[str, tuple[str, ...]] | None = None,
) -> ReasoningTierResolution:
    """Apply upstream, bundled-catalog, then user provenance in that order.

    A catalog row is a client's menu: its ``client_presets`` (Codex ``ultra``)
    are names the client maps before sending, so a Source's wire tiers never
    include them (#2539).
    """

    if supported_parameters is not None and any(
        parameter.strip().lower() in _UPSTREAM_REASONING_PARAMETERS
        for parameter in supported_parameters
    ):
        defaults = PROTOCOL_REASONING_EFFORT_DEFAULTS.get(protocol)
        if defaults is not None:
            return ReasoningTierResolution(defaults, "upstream")

    catalog_efforts = (
        bundled_catalog_reasoning_efforts_for_model(model_id)
        if catalog_efforts_by_model is None
        else catalog_efforts_by_model.get(model_id)
    )
    if catalog_efforts is not None:
        return ReasoningTierResolution(
            catalog_wire_efforts(model_id, catalog_efforts, client_presets_by_model), "catalog"
        )

    if existing_source == "user" and existing_efforts:
        return ReasoningTierResolution(tuple(existing_efforts), "user")
    return ReasoningTierResolution((), None)


def catalog_wire_efforts(
    model_id: str,
    efforts: Sequence[str],
    client_presets_by_model: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """A catalog-sourced tier list without the client presets of that model's row.

    Also applied when reading a persisted ``catalog`` list, which a released
    build may have written before presets were known.
    """

    presets = (
        bundled_catalog_client_presets_by_model() if client_presets_by_model is None else client_presets_by_model
    ).get(model_id, ())
    return tuple(effort for effort in efforts if effort not in presets)
