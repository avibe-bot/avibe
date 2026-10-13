"""Single provenance ladder for Source-model reasoning effort declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from vibe.backend_model_catalog import (
    PROTOCOL_REASONING_EFFORT_DEFAULTS,
    bundled_catalog_wire_efforts_by_model,
    catalog_row_value,
    without_client_presets,
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
) -> ReasoningTierResolution:
    """Apply upstream, bundled-catalog, then user provenance in that order.

    The catalog row is found by the model's id, or by the id a snapshot, tag or
    provider prefix folds to (``catalog_model_id``), and it bounds the protocol
    defaults an upstream signal stands for. A catalog row is a client's menu: its ``client_presets`` (Codex ``ultra``)
    are names the client maps before sending, so a Source's wire tiers never
    include them (#2539). ``catalog_efforts_by_model`` is therefore the wire
    index, ``bundled_catalog_wire_efforts_by_model()``.
    """

    catalog_efforts = catalog_row_value(
        bundled_catalog_wire_efforts_by_model() if catalog_efforts_by_model is None else catalog_efforts_by_model,
        model_id,
    )
    if supported_parameters is not None and any(
        parameter.strip().lower() in _UPSTREAM_REASONING_PARAMETERS
        for parameter in supported_parameters
    ):
        defaults = PROTOCOL_REASONING_EFFORT_DEFAULTS.get(protocol)
        if defaults is not None:
            # A generic reasoning parameter says the upstream takes the protocol's levels; the model's own row
            # bounds them, so the hop declares exactly the levels its model has (Opus 4.6 has no xhigh).
            return ReasoningTierResolution(
                defaults if catalog_efforts is None else tuple(effort for effort in defaults if effort in catalog_efforts),
                "upstream",
            )

    if catalog_efforts is not None:
        return ReasoningTierResolution(catalog_efforts, "catalog")

    if existing_source == "user" and existing_efforts:
        return ReasoningTierResolution(tuple(existing_efforts), "user")
    return ReasoningTierResolution((), None)


def catalog_wire_efforts(model_id: str, efforts: Sequence[str]) -> tuple[str, ...]:
    """A catalog-sourced tier list without the client presets of that model's row.

    Also applied when reading a persisted ``catalog`` list, which a released
    build may have written before presets were known.
    """

    return tuple(without_client_presets((model_id,), efforts))


def rederive_persisted_tiers(
    *,
    protocol: str,
    model_id: str,
    efforts: Sequence[str],
    source: ReasoningEffortsSource,
    catalog_efforts_by_model: Mapping[str, tuple[str, ...]],
) -> ReasoningTierResolution | None:
    """A saved Source declaration derived again by this ladder, or None to keep it as saved.

    An earlier build derived rows by its own rules: 3.2.0 matched only exact
    ids, and gave every model behind a generic reasoning parameter the whole
    protocol default list. A row the bundled catalog describes is derived again
    when this ladder made it (``catalog``, ``upstream``) or found nothing for it
    (no source and no tiers). An upstream row keeps its upstream signal. A
    user's list waits for discovery, which reports when the catalog outranks it.
    """

    if catalog_row_value(catalog_efforts_by_model, model_id) is None:
        return None
    if source == "upstream":
        return resolve_reasoning_tiers(
            protocol=protocol,
            model_id=model_id,
            supported_parameters=("reasoning",),
            catalog_efforts_by_model=catalog_efforts_by_model,
        )
    if source == "catalog" or (source is None and not efforts):
        return resolve_reasoning_tiers(
            protocol=protocol, model_id=model_id, catalog_efforts_by_model=catalog_efforts_by_model,
        )
    return None
