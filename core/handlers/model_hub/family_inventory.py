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
from typing import Callable, Iterable, Literal, Mapping

from config.v2_config import ModelHubAgentSupplyConfig, ModelHubBackendModelConfig, ModelHubConfig, ModelHubSourceConfig, normalized_model_hub_override
from .events import contains_credential_material
from .identifiers import canonical_model_id
from .retry import source_identity
from .state_file import write_state_document


logger = logging.getLogger(__name__)
FAMILY_INVENTORY_FILE = "model_hub_family_inventory.json"


def _scope(source: ModelHubSourceConfig) -> str:
    return hashlib.sha256(json.dumps(source_identity(source), separators=(",", ":")).encode()).hexdigest()


class FamilyModelRow(ModelHubBackendModelConfig):
    """An execution-only overlay; ordinary stored rows are always explicit here."""


def withdrawn_builtin_ids(backend: str, current_ids: Callable[[], Iterable[str]], *, offline: bool = False) -> set[str]:
    from vibe.backend_model_catalog import (
        load_bundled_catalog, load_cached_remote_catalog, retired_backend_model_ids, unlisted_retired_backend_model_ids,
    )

    retired = (retired_backend_model_ids(backend, load_bundled_catalog())
               | retired_backend_model_ids(backend, load_cached_remote_catalog(schedule_refresh=not offline))
               | unlisted_retired_backend_model_ids(backend))
    return set(retired).difference(current_ids()) if retired else set()


def hidden_retired_rows(agent: ModelHubAgentSupplyConfig, withdrawn: set[str], requested: str = "") -> set[str]:
    return {row.id for row in agent.models if (row.origin == "builtin" or isinstance(row, FamilyModelRow))
            and row.id in withdrawn and row.id != requested and normalized_model_hub_override(agent.routes.get(row.id)) is None}


@dataclass(frozen=True)
class Listing:
    scope: str
    observed_at: str
    ids: tuple[str, ...]
    menu_evidence: bool = True
    retired_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FamilyInventory:
    status: Literal["missing", "valid", "damaged"]
    listings: Mapping[str, Listing] = field(default_factory=dict)
    history: Mapping[str, tuple[ModelHubBackendModelConfig, ...]] = field(default_factory=dict)

    def route_view(self, config: ModelHubConfig, backend: str) -> ModelHubConfig:
        """Add historical execution metadata only to this read; never persist implicit menu choices."""
        agent = config.agents[backend]
        if agent.mode != "hub" or not self.history.get(backend):
            return config
        explicit = {row.id for row in agent.models} | set(agent.removed_model_ids)
        additions = [row for row in self.history[backend] if row.id not in explicit]
        if not additions:
            return config
        return replace(config, agents={**config.agents, backend: replace(agent, models=[*agent.models, *additions])})

    def receipt(self, source: ModelHubSourceConfig) -> Listing | None:
        listing = self.listings.get(source.id)
        if (source.supply_channel != "hub" or not source.credential_ref or listing is None
                or listing.scope != _scope(source)):
            return None
        return listing

    def listed(self, source: ModelHubSourceConfig) -> tuple[str, ...] | None:
        listing = self.receipt(source)
        return listing.ids if listing is not None and listing.menu_evidence else None

    def observed(self, source: ModelHubSourceConfig, ids: tuple[str, ...], at: str) -> FamilyInventory:
        if self.status == "damaged":
            raise ValueError("unreadable Hub family inventory")
        if not source.credential_ref or source.supply_channel != "hub":
            raise ValueError("a Hub listing requires a bound Source")
        ids = _validated_ids(list(ids))
        prior = self.receipt(source)
        # A retirement stays manageable after withdrawal on this account. A
        # retained routing row alone must not disclose it to the next account.
        witnessed = set(ids) | (set(prior.ids + prior.retired_ids) if prior else set())
        retired_ids = tuple(row.id for row in source.models if row.retired and row.id in witnessed)
        listing = Listing(_scope(source), at, ids,
                          menu_evidence=not (source.kind == "api_key" and bool(source.verification_pending)),
                          retired_ids=retired_ids)
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
        explicit = {row.id for row in agent.models if not isinstance(row, FamilyModelRow)}
        withdrawn = {row.id for row in self.history.get(backend, ()) if row.id not in explicit} - self.supply(config, backend).keys()
        return {mid for mid in withdrawn if normalized_model_hub_override(agent.routes.get(mid)) is None}

    def codex_launch_rows(self, config: ModelHubConfig, hidden: set[str]) -> list[dict]:
        """Visibility affects listing only; saved launches still need each row's metadata."""
        agent = self.route_view(config, "codex").agents["codex"]
        excluded = hidden.union(agent.removed_model_ids)
        return [{**row.to_payload(), "menu_visible": row.id not in excluded} for row in agent.models]

    def offline_codex_launch_rows(self, config: ModelHubConfig) -> list[dict]:
        from vibe.backend_model_catalog import backend_builtin_models

        withdrawn = withdrawn_builtin_ids("codex", lambda: (row["id"] for row in backend_builtin_models(
            "codex", schedule_refresh=False)), offline=True)
        agent = self.route_view(config, "codex").agents["codex"]
        return self.codex_launch_rows(config, self.hidden(config, "codex") | hidden_retired_rows(agent, withdrawn))

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
            if source.state.status == "needs_action" and self.receipt(source) is not None:
                sources.append(replace(source, models=[]))
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
            if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 2:
                raise ValueError
            listings, history = payload["listings"], payload["history"]
            if not isinstance(listings, dict) or not isinstance(history, dict):
                raise ValueError
            parsed = {}
            for sid, row in listings.items():
                if (not isinstance(row, dict) or not isinstance(row.get("scope"), str)
                        or not isinstance(row.get("observed_at"), str)):
                    raise ValueError
                evidence = row["menu_evidence"]
                if type(evidence) is not bool:
                    raise ValueError
                from .resolver import parse_model_hub_timestamp
                parse_model_hub_timestamp(row["observed_at"])
                parsed[sid] = Listing(row["scope"], row["observed_at"], _validated_ids(row["ids"]), evidence,
                                      _validated_ids(row.get("retired_ids", [])))
            rows_by_backend = {}
            for backend, rows in history.items():
                if backend not in {"claude", "codex"} or not isinstance(rows, list):
                    raise ValueError
                checked = tuple(FamilyModelRow.from_payload(row) for row in rows)
                _validated_ids([row.id for row in checked])
                rows_by_backend[backend] = checked
            return cls("valid", parsed, rows_by_backend)
        except FileNotFoundError:
            return cls("missing")
        except (KeyError, ValueError, OSError, TypeError, RecursionError):
            logger.warning("Cannot read Hub family inventory; retaining the original file and saved routes")
            return cls("damaged")

    def save(self, path: Path) -> None:
        if self.status != "valid":
            raise ValueError("no writable Hub family inventory")
        write_state_document(path, {
            "version": 2,
            "listings": {sid: {"scope": row.scope, "observed_at": row.observed_at, "ids": row.ids,
                               "menu_evidence": row.menu_evidence, "retired_ids": row.retired_ids}
                         for sid, row in self.listings.items()},
            "history": {key: [row.to_payload() for row in rows] for key, rows in self.history.items()},
        })


def _validated_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or not all(
        isinstance(value, str) and canonical_model_id(value) == value and not contains_credential_material(value)
        for value in values
    ) or len(set(values)) != len(values):
        raise ValueError("invalid Hub family inventory")
    return tuple(values)
