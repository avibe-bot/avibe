import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from config.v2_config import DEFAULT_AGENT_BACKEND

logger = logging.getLogger(__name__)


@dataclass
class PlatformRoute:
    default: str = DEFAULT_AGENT_BACKEND
    overrides: Dict[str, str] = field(default_factory=dict)


class AgentRouter:
    """Resolve which agent should serve a given message context."""

    def __init__(
        self,
        platform_routes: Dict[str, PlatformRoute],
        global_default: str = DEFAULT_AGENT_BACKEND,
    ):
        self.platform_routes = platform_routes
        self.global_default = global_default

    @classmethod
    def from_file(
        cls, file_path: Optional[str], *, platform: str, fallback_backend: str = DEFAULT_AGENT_BACKEND
    ) -> "AgentRouter":
        routes: Dict[str, PlatformRoute] = {}
        global_default = fallback_backend

        # File-based routing removed; keep defaults only.
        routes.setdefault(platform, PlatformRoute(default=global_default))
        return cls(routes, global_default=global_default)

    @staticmethod
    def _load_file(path: str) -> Dict:
        _, ext = os.path.splitext(path)
        if ext.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "PyYAML is required to parse YAML agent route files. "
                    "Install with `pip install pyyaml` or use JSON."
                ) from exc
            with open(path, "r") as f:
                return yaml.safe_load(f) or {}
        with open(path, "r") as f:
            return json.load(f)

    def resolve(self, platform: str, channel_id: str) -> str:
        platform_route = self.platform_routes.get(platform)
        if not platform_route:
            return self.global_default
        return platform_route.overrides.get(channel_id, platform_route.default)
