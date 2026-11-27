"""Last complete account inventories and Avibe-owned menu rows, not routing configuration.

One controller writer. Replacing a credential changes the lookup key immediately,
even if reading the new credential fails. No CLI, disk or network work on read.
Older installations have no evidence: never bootstrap it from their routing rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from config.v2_config import ModelHubConfig, ModelHubSourceConfig, normalized_model_hub_override
from .retry import source_identity
from .state_file import write_state_document


def inventory_scope(source: ModelHubSourceConfig) -> str:
    # Only the digest of the existing configuration identity goes on disk.
    return hashlib.sha256(json.dumps(source_identity(source), separators=(",", ":")).encode()).hexdigest()


@dataclass
class MenuEvidence:
    inventories: dict[str, dict] = field(default_factory=dict)
    automatic: dict[str, list[str]] = field(default_factory=dict)
    # A bundled fallback explicitly restored or edited is no longer a fallback.
    chosen_builtins: dict[str, list[str]] = field(default_factory=dict)

    def listed(self, source: ModelHubSourceConfig) -> frozenset[str] | None:
        row = self.inventories.get(source.id)
        if not source.credential_ref or source.supply_channel != "hub" or not row or row["scope"] != inventory_scope(source):
            return None
        return frozenset(row["ids"])

    def observe(self, source: ModelHubSourceConfig, ids: list[str]) -> None:
        self.inventories[source.id] = {"scope": inventory_scope(source), "ids": sorted(set(ids))}

    def hidden(self, config: ModelHubConfig, backend: str, pins: set[str]) -> set[str]:
        """Only auto-owned rows follow withdrawal; saved routes remain untouched."""
        agent = config.agents[backend]
        if agent.mode != "hub":
            return set()
        sources = [s for s in config.sources if s.id in agent.sources.order
                   and config.source_eligible_for_backend(s, backend)]
        by_protocol: dict[str, set[str]] = {}
        known = False
        for source in sources:
            listed = self.listed(source)
            if listed is None:
                continue
            known = True
            by_protocol.setdefault(source.protocol, set()).update(
                row.id for row in source.models if not row.retired and row.id in listed
            )
        supplied = set().union(*by_protocol.values())
        owned = set(self.automatic.get(backend, ()))
        chosen = set(self.chosen_builtins.get(backend, ()))
        return {
            row.id for row in agent.models
            if (row.id in owned or (known and agent.menu_kind == "fixed" and row.origin == "builtin"))
            and row.id not in chosen
            and row.id not in pins and normalized_model_hub_override(agent.routes.get(row.id)) is None
            and row.id not in (by_protocol.get(row.native_protocol, set()) if backend == "opencode" else supplied)
        }

    @classmethod
    def load(cls, path: Path) -> MenuEvidence:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid model menu evidence")
        inventories, automatic = payload["inventories"], payload["automatic"]
        chosen = payload.get("chosen_builtins", {})
        if not isinstance(inventories, dict) or not isinstance(automatic, dict) or not isinstance(chosen, dict):
            raise ValueError("invalid model menu evidence")
        def ids(value):
            return isinstance(value, list) and all(isinstance(i, str) and i for i in value)
        if not all(isinstance(row, dict) and isinstance(row.get("scope"), str) and ids(row.get("ids"))
                   for row in inventories.values()) or not all(ids(rows) for rows in (*automatic.values(), *chosen.values())):
            raise ValueError("invalid model menu evidence")
        return cls(inventories, automatic, chosen)

    def save(self, path: Path) -> None:
        write_state_document(path, {"version": 1, "inventories": self.inventories, "automatic": self.automatic,
                                    "chosen_builtins": self.chosen_builtins})
