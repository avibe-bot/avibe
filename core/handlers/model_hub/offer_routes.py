"""Explicit route overrides for read-time offers. Older binaries ignore this file."""
from __future__ import annotations

import json
from pathlib import Path

from config.v2_config import ModelHubRouteConfig
from .events import contains_credential_material
from .identifiers import canonical_model_id
from .state_file import write_state_document


class OfferRoutePreferences:
    def __init__(self, directory: Path, backend: str):
        self.path = directory / f"hub_{backend}_offer_routes.json"
        self.routes: dict[str, ModelHubRouteConfig] = {}
        self.damaged = False
        try:
            raw = json.loads(self.path.read_bytes())
            if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("routes"), dict):
                raise ValueError("invalid offer routes")
            routes = {}
            for mid, entry in raw["routes"].items():
                if not mid or canonical_model_id(mid) != mid or contains_credential_material(mid):
                    raise ValueError("invalid offer identity")
                route = ModelHubRouteConfig.from_payload(entry)
                if not route.hops:
                    raise ValueError("empty explicit route")
                routes[mid] = route
            self.routes = routes
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, OSError, RecursionError):
            self.damaged = True

    def set(self, mid: str, route: ModelHubRouteConfig | None) -> None:
        if self.damaged:
            raise OSError("unreadable explicit offer routes")
        updated = dict(self.routes)
        if route is None:
            updated.pop(mid, None)
        else:
            updated[mid] = route
        if updated != self.routes:
            write_state_document(self.path, {"version": 1, "routes": {key: value.to_payload() for key, value in updated.items()}})
            self.routes = updated
