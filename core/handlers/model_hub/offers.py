"""The evidence behind Hub offers, never the menu or an execution history."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from config.v2_config import ModelHubConfig
from .capability_evidence import identity_digest
from .events import contains_credential_material
from .identifiers import canonical_model_id
from .state_file import write_state_document


@dataclass(frozen=True)
class CompleteListing:
    identity: str
    model_ids: tuple[str, ...]
    observed_at: str


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
                result[sid] = CompleteListing(digest, tuple(ids), at)
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
                or not source.credential_ref or source.verification_pending or source.state.status == "needs_action"
                or not config.source_eligible_for_backend(source, backend)
                or receipt is None or identity_digest(source) != receipt.identity):
            continue
        ids = set(receipt.model_ids)
        sources.append(replace(source, models=[row for row in source.models if not row.retired and row.id in ids]))
    return replace(config, sources=sources)
