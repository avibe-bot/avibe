"""Snapshot of the listings that may contribute automatic Hub menu rows.

Routing config deliberately retains models which a listing no longer contains.
The menu must never reconstruct an observation from that config. A missing file
means no observations yet; a damaged file means ownership is unknown and must
not be overwritten. Readers only use a previously published snapshot.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Mapping

from config.v2_config import ModelHubConfig, ModelHubSourceConfig, normalized_model_hub_override
from .events import contains_credential_material
from .identifiers import canonical_model_id
from .retry import source_identity
from .state_file import write_state_document


logger = logging.getLogger(__name__)
FAMILY_INVENTORY_FILE = "model_hub_family_inventory.json"


def _scope(source: ModelHubSourceConfig) -> str:
    return hashlib.sha256(json.dumps(source_identity(source), separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Listing:
    scope: str
    observed_at: str
    ids: tuple[str, ...]


@dataclass(frozen=True)
class FamilyInventory:
    status: Literal["missing", "valid", "damaged"]
    listings: Mapping[str, Listing] = field(default_factory=dict)
    automatic: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def listed(self, source: ModelHubSourceConfig) -> tuple[str, ...] | None:
        listing = self.listings.get(source.id)
        if (source.supply_channel != "hub" or not source.credential_ref or listing is None
                or listing.scope != _scope(source)):
            return None
        return listing.ids

    def observed(self, source: ModelHubSourceConfig, ids: tuple[str, ...], at: str) -> FamilyInventory:
        if self.status == "damaged":
            raise ValueError("unreadable Hub family inventory")
        if not source.credential_ref or source.supply_channel != "hub":
            raise ValueError("a Hub listing requires a bound Source")
        listing = Listing(_scope(source), at, _validated_ids(list(ids)))
        return replace(self, status="valid", listings={**self.listings, source.id: listing})

    def hidden(self, config: ModelHubConfig, backend: str) -> set[str]:
        """Models retained for execution but not offered to a new picker."""
        agent = config.agents[backend]
        if agent.mode != "hub":
            return set()
        if self.status == "damaged":
            # Never guess which provider rows were deliberate. An explicit
            # route still identifies a choice; individual stored selections
            # are displayed by their own consumers, not relisted globally.
            return {row.id for row in agent.models if row.origin in {"builtin", "provider"}
                    and normalized_model_hub_override(agent.routes.get(row.id)) is None}
        return set(self.automatic.get(backend, ())).difference(self.supply(config, backend))

    def codex_launch_rows(self, config: ModelHubConfig, hidden: set[str]) -> list[dict]:
        """Visibility affects listing only; saved launches still need each row's metadata."""
        agent = config.agents["codex"]
        excluded = hidden.union(agent.removed_model_ids)
        return [{**row.to_payload(), "menu_visible": row.id not in excluded} for row in agent.models]

    def candidate_config(self, config: ModelHubConfig) -> ModelHubConfig:
        """Do not offer a retained execution hop as a current supplier.

        Before the first observation on a legacy install, keep the existing
        manual Add policy. Once an identity has been observed, losing or
        replacing that identity cannot turn its retained routes into offers.
        This projection never mutates the resolver's config.
        """
        sources = []
        for source in config.sources:
            if source.supply_channel != "hub" or source.protocol == "search":
                sources.append(source)
                continue
            listed = self.listed(source)
            if (self.status != "damaged" and listed is None and source.id not in self.listings
                    and source.credential_generation is None):
                sources.append(source)
                continue
            ids = frozenset(listed or ())
            sources.append(replace(source, models=[row for row in source.models
                                                   if row.provenance == "manual" or row.id in ids]))
        return replace(config, sources=sources)

    def supply(self, config: ModelHubConfig, backend: str) -> dict[str, list[str]]:
        """Model to contributing Sources, in assignment order; a missing listing contributes nothing."""
        by_id = {source.id: source for source in config.sources}
        result: dict[str, list[str]] = {}
        for sid in config.effective_source_order(backend):
            source = by_id.get(sid)
            if (source is None or not config.source_eligible_for_backend(source, backend)
                    or source.state.status == "needs_action"):
                continue
            retired = {model.id for model in source.models if model.retired}
            for model_id in self.listed(source) or ():
                if model_id not in retired:
                    result.setdefault(model_id, []).append(sid)
        return result

    @classmethod
    def load(cls, path: Path) -> FamilyInventory:
        try:
            payload = json.loads(path.read_bytes())
            if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
                raise ValueError
            listings, automatic = payload["listings"], payload["automatic"]
            if not isinstance(listings, dict) or not isinstance(automatic, dict):
                raise ValueError
            parsed = {}
            for sid, row in listings.items():
                if (not isinstance(row, dict) or not isinstance(row.get("scope"), str)
                        or not isinstance(row.get("observed_at"), str)):
                    raise ValueError
                parsed[sid] = Listing(row["scope"], row["observed_at"], _validated_ids(row["ids"]))
            return cls("valid", parsed, {key: frozenset(_validated_ids(ids)) for key, ids in automatic.items()})
        except FileNotFoundError:
            return cls("missing")
        except (KeyError, ValueError, OSError, TypeError, RecursionError):
            logger.warning("Cannot read Hub family inventory; retaining the original file and saved routes")
            return cls("damaged")

    def save(self, path: Path) -> None:
        if self.status != "valid":
            raise ValueError("no writable Hub family inventory")
        write_state_document(path, {
            "version": 1,
            "listings": {sid: {"scope": row.scope, "observed_at": row.observed_at, "ids": row.ids}
                         for sid, row in self.listings.items()},
            "automatic": {key: sorted(ids) for key, ids in self.automatic.items()},
        })


def _validated_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(
        isinstance(value, str) and canonical_model_id(value) == value and not contains_credential_material(value)
        for value in values
    ) or len(set(values)) != len(values):
        raise ValueError("invalid Hub family inventory")
    return tuple(values)
