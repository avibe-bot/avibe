"""Execution intent stored with an explicit native Hub pick, never in the global menu."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig, ModelHubRouteConfig, ModelHubRouteHopConfig
from .capability_evidence import identity_digest
from .offers import NATIVE_OFFER_BACKENDS
from .resolver import OfferedModel, SavedSelectionModel


PICK_METADATA_KEY = "saved_native_hub_pick_v1"


def read_session_pick(session_id: str, backend: str, model: str) -> SavedHubPick | None:
    """Read the selected Session's own row, not metadata supplied with a message."""
    from config import paths
    from sqlalchemy import select
    from storage.db import get_cached_sqlite_engine
    from storage.models import agent_sessions

    if backend not in NATIVE_OFFER_BACKENDS or not session_id or not paths.get_sqlite_state_path().is_file():
        return None
    with get_cached_sqlite_engine().connect() as conn:
        metadata = conn.execute(select(agent_sessions.c.metadata_json).where(
            agent_sessions.c.id == session_id, agent_sessions.c.agent_backend == backend, agent_sessions.c.model == model,
        )).scalar_one_or_none()
    if not metadata:
        return None
    try:
        fields = json.loads(metadata)
    except (ValueError, TypeError):
        return None
    pick = SavedHubPick.from_payload(fields.get(PICK_METADATA_KEY)) if isinstance(fields, dict) else None
    return pick if pick and (pick.backend, pick.model.id) == (backend, model) else None


def record_explicit_pick(metadata: dict, backend: str, model: str | None) -> None:
    """Replace only this object's selection; callers must strip untrusted incoming metadata first."""
    previous = SavedHubPick.from_payload(metadata.get(PICK_METADATA_KEY))
    if model and backend in NATIVE_OFFER_BACKENDS:
        from .service import capture_explicit_native_pick

        pick = capture_explicit_native_pick(backend, model)
        if pick is not None:
            metadata[PICK_METADATA_KEY] = pick.to_payload()
            return
    # The caller explicitly supplied the model. A current reoffer can replace
    # its old binding, but a form echo of a withdrawn selection must not erase it.
    if previous is None or (previous.backend, previous.model.id) != (backend, model):
        metadata.pop(PICK_METADATA_KEY, None)


@dataclass(frozen=True)
class SelectedHop:
    hop: ModelHubRouteHopConfig
    identity: str


@dataclass(frozen=True)
class SavedHubPick:
    backend: str
    model: ModelHubBackendModelConfig
    hops: tuple[SelectedHop, ...]
    manual: bool = False
    cache_ttl: str | None = None

    @classmethod
    def from_offer(cls, config: ModelHubConfig, backend: str, offer: OfferedModel,
                   override: ModelHubRouteConfig | None = None) -> SavedHubPick:
        sources = {source.id: source for source in config.sources}
        targets = override.hops if override else offer.listed_hops
        return cls(backend, ModelHubBackendModelConfig.from_payload(offer.to_payload()), tuple(
            SelectedHop(hop, identity_digest(sources[hop.source_id])) for hop in targets if hop.source_id in sources
        ), override is not None, override.cache_ttl if override else None)

    def to_payload(self) -> dict:
        return {"version": 1, "backend": self.backend, "model": self.model.to_payload(),
                **({"manual": True, "cache_ttl": self.cache_ttl} if self.manual else {}),
                **({"unavailable_at_switch": True} if not self.hops else {}),
                "hops": [{**entry.hop.to_payload(), "identity": entry.identity} for entry in self.hops]}

    def route_key(self) -> str:
        """Canonical private gateway key; a change of binding must not reuse a route handle."""
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_payload(cls, raw: object) -> SavedHubPick | None:
        if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1
                or raw.get("backend") not in NATIVE_OFFER_BACKENDS or not isinstance(raw.get("hops"), list)):
            return None
        try:
            model = ModelHubBackendModelConfig.from_payload(raw["model"])
            hops = []
            for item in raw["hops"]:
                if not isinstance(item, dict) or set(item) != {"source_id", "model_id", "identity"}:
                    return None
                identity = item["identity"]
                if not isinstance(identity, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
                    return None
                hop = ModelHubRouteHopConfig.from_payload({key: item[key] for key in ("source_id", "model_id")})
                hops.append(SelectedHop(hop, identity))
            # A switch without a serving Source records a refusal, never a
            # wildcard authorization for a future offer with the same name.
            manual = raw.get("manual", False)
            ttl = raw.get("cache_ttl")
            if type(manual) is not bool or ttl not in (None, "5m", "30m", "1h", "12h") or (ttl and not manual):
                return None
            keys = [(entry.hop.source_id, entry.hop.model_id if manual else None) for entry in hops]
            if (not hops and raw.get("unavailable_at_switch") is not True) or len(set(keys)) != len(keys):
                return None
            return cls(raw["backend"], model, tuple(hops), manual, ttl)
        except (ValueError, TypeError, KeyError):
            return None

    def execution_view(self, config: ModelHubConfig, backend: str, model_id: str) -> ModelHubConfig:
        """One selection only. A saved reference cannot assign a Source or transfer a private grant."""
        if backend != self.backend or model_id != self.model.id or config.agents[backend].mode != "hub":
            return config
        agent = config.agents[backend]
        sources = {source.id: source for source in config.sources}
        assigned = {sid: index for index, sid in enumerate(config.effective_source_order(backend))}
        ordered = self.hops if self.manual else sorted(self.hops, key=lambda h: assigned.get(h.hop.source_id, len(assigned)))
        hops = tuple(entry.hop for entry in ordered if entry.hop.source_id in assigned
                     and (source := sources.get(entry.hop.source_id)) is not None
                     and source.supply_channel == "hub" and source.credential_ref
                     and (not source.verification_pending or source.kind == "subscription")
                     and config.source_eligible_for_backend(source, backend) and identity_digest(source) == entry.identity)
        # Keep even the no-hop row private to this turn: the normal resolver reports
        # unavailable rather than selecting the Agent's default or an unrelated Source.
        selected = SavedSelectionModel(**self.model.to_payload(), listed_hops=hops)
        rows = [row for row in agent.models if row.id != model_id]
        # A later global route override for this ID isn't one of this pick's authorized
        # suppliers. Keep it in the public config, but not in this private execution view.
        routes = {key: route for key, route in agent.routes.items() if key != model_id}
        if self.manual and hops:
            routes[model_id] = ModelHubRouteConfig.from_payload({"hops": [hop.to_payload() for hop in hops],
                                                                 "cache_ttl": self.cache_ttl})
        return replace(config, agents={**config.agents, backend: replace(agent, models=[*rows, selected], routes=routes)})
