"""The evidence behind Hub offers, never the menu or an execution history."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from config.v2_config import ModelHubConfig, ModelHubModelConfig
from .reasoning_tiers import resolve_reasoning_tiers
from .capability_evidence import identity_digest
from .events import contains_credential_material
from .identifiers import canonical_model_id
from .state_file import write_state_document


# These native pickers expose a single provider family. Vibey consumes the same
# evidence for its Add candidates, but never auto-equips a model.
NATIVE_OFFER_BACKENDS = ("claude", "codex")
OFFER_CANDIDATE_BACKENDS = (*NATIVE_OFFER_BACKENDS, "vibey")


@dataclass(frozen=True)
class CompleteListing:
    identity: str
    model_ids: tuple[str, ...]
    observed_at: str
    reasoning_ids: tuple[str, ...] = ()


class ListingFile:
    """Stores observations only. Unreadable evidence suppresses offers, never user choices."""

    def __init__(self, path: Path):
        self.path = path
        self.damaged = False

    def load(self) -> dict[str, CompleteListing]:
        try:
            data = json.loads(self.path.read_bytes())
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError
            rows = data["sources"]
            if not isinstance(rows, dict):
                raise ValueError
            result = {}
            for sid, row in rows.items():
                if not isinstance(row, dict) or not re.fullmatch(r"src_[a-z0-9]{8,}", sid):
                    raise ValueError
                digest, ids, at = row["identity"], row["model_ids"], row["observed_at"]
                if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    raise ValueError
                if not isinstance(ids, list) or not all(isinstance(mid, str) and canonical_model_id(mid) == mid
                        and not contains_credential_material(mid) for mid in ids) or len(set(ids)) != len(ids):
                    raise ValueError
                datetime.fromisoformat(at)
                reasoning = row.get("reasoning_ids", [])
                if not isinstance(reasoning, list) or not set(reasoning).issubset(ids):
                    raise ValueError
                result[sid] = CompleteListing(digest, tuple(ids), at, tuple(reasoning))
            return result
        except FileNotFoundError:
            return {}
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            self.damaged = True
            logging.getLogger(__name__).warning("Hub offer observations unreadable; preserving the original file")
            return {}

    def save(self, listings: Mapping[str, CompleteListing]) -> None:
        if self.damaged:
            raise OSError("unreadable Hub offer observations")
        write_state_document(self.path, {"version": 1, "sources": {sid: asdict(row) for sid, row in listings.items()}})


def listed_supplier_view(config: ModelHubConfig, backend: str, listings: Mapping[str, CompleteListing]) -> ModelHubConfig:
    """Filter a read-only view, not the stored routing union. No receipt means no offer."""
    assigned = set(config.effective_source_order(backend))
    sources = []
    for source in config.sources:
        receipt = listings.get(source.id)
        if (source.id not in assigned or source.supply_channel != "hub" or source.protocol == "search"
                or not source.credential_ref or (source.verification_pending and source.kind != "subscription")
                or source.state.status == "needs_action"
                or not config.source_eligible_for_backend(source, backend)
                or receipt is None or identity_digest(source) != receipt.identity):
            continue
        stored = {row.id: row for row in source.models}
        reasoning_ids = set(receipt.reasoning_ids)
        rows = []
        for mid in receipt.model_ids:
            row = stored.get(mid)
            if row is not None and row.retired:
                continue
            if row is None:
                # Inventory and routing edits are independent. A guard may have
                # retained the old routing rows after a complete new observation.
                tiers = resolve_reasoning_tiers(protocol=source.protocol, model_id=mid,
                    supported_parameters=("reasoning",) if mid in reasoning_ids else None)
                row = ModelHubModelConfig(id=mid, provenance="discovered", discovered_at=receipt.observed_at,
                    reasoning_efforts=list(tiers.efforts), reasoning_efforts_source=tiers.source)
            rows.append(row)
        sources.append(replace(source, models=rows))
    return replace(config, sources=sources)
