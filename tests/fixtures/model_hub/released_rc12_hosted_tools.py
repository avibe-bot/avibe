"""Unchanged hosted-tool loader from gh-v3.2.2rc12:config/v2_config.py.

Only its imports are supplied here; the unchanged Source-id/model-choice/order
helpers remain shared. This snapshot tests downgrade of optional settings:
rc12 retains tool switches and both orders, but not the new service opt-outs.
"""

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from config.v2_config import (
    MODEL_HUB_HOSTED_TOOLS,
    MODEL_HUB_SEARCH_TOOLS,
    ModelHubImageModelChoice,
    model_hub_executor_order,
    model_hub_service_order,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelHubHostedToolConfig:
    """One hosted tool's user settings (``vibey-hosted-tools.md`` §3.3).

    ``enabled`` defaults to on. ``executor_order`` (search tools) is the Source order the user wants executors
    tried in, None for automatic; ``service_order`` (``web_search``) is the server-owned automatic order of
    tool-only search Sources; ``default_image`` (``image_generate``, persisted as ``default_model``) is the
    chosen default image model, None for the catalog default. Both are saved values, never resolved ones.
    """

    enabled: bool = True
    executor_order: Optional[tuple[str, ...]] = None
    service_order: Optional[tuple[str, ...]] = None
    default_image: Optional[ModelHubImageModelChoice] = None

    @classmethod
    def from_payload(cls, payload: object, *, tool: str) -> "ModelHubHostedToolConfig":
        """Read one entry; fields it does not know are ignored with a warning, the ones it knows are kept."""

        if not isinstance(payload, dict):
            raise ValueError(f"Config 'model_hub.hosted_tools.{tool}' must be an object")
        known = {"enabled"} | (
            ({"executor_order", "service_order"} if tool == "web_search" else {"executor_order"})
            if tool in MODEL_HUB_SEARCH_TOOLS
            else {"default_model"}
        )
        unknown = set(payload) - known
        if unknown:
            logger.warning("Config 'model_hub.hosted_tools.%s' ignores unknown fields %s", tool, sorted(unknown))
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"Config 'model_hub.hosted_tools.{tool}.enabled' must be a boolean")
        # Only the fields this tool knows are read; another tool's field was ignored above. A field is decided by its
        # presence: the writer omits an unset one, so a present null is damage and goes through the grammar.
        has_order = "executor_order" in known and "executor_order" in payload
        has_service_order = "service_order" in known and "service_order" in payload
        has_default = "default_model" in known and "default_model" in payload
        return cls(
            enabled=enabled,
            executor_order=model_hub_executor_order(payload["executor_order"], tool=tool) if has_order else None,
            service_order=model_hub_service_order(payload["service_order"], tool=tool) if has_service_order else None,
            default_image=(
                ModelHubImageModelChoice.from_payload(payload["default_model"], tool=tool) if has_default else None
            ),
        )

    def to_payload(self) -> dict:
        out: dict[str, Any] = {"enabled": self.enabled}
        if self.executor_order is not None:
            out["executor_order"] = list(self.executor_order)
        if self.service_order is not None:
            out["service_order"] = list(self.service_order)
        if self.default_image is not None:
            out["default_model"] = self.default_image.to_payload()
        return out


def _hosted_tools_from_payload(section: Mapping[str, Any]) -> dict[str, ModelHubHostedToolConfig]:
    """Read ``model_hub.hosted_tools`` as an optional feature that degrades on its own.

    Absent means every tool takes its defaults (on). An unknown tool key, or an unknown field inside an entry, is
    ignored with a warning, and the fields the loader knows are kept, so a user's opt-out survives. A broken section
    or entry turns its tools off, with a warning: hosted tools reach third parties and cost money, so damage never
    turns one on. Sources and Agents still load.
    """

    if "hosted_tools" not in section:
        return {}
    payload = section["hosted_tools"]
    # Present but not an object, ``null`` included, is damage, never absence.
    if not isinstance(payload, dict):
        logger.warning("Config 'model_hub.hosted_tools' is not an object; hosted tools are off")
        return {tool: ModelHubHostedToolConfig(enabled=False) for tool in MODEL_HUB_HOSTED_TOOLS}
    tools: dict[str, ModelHubHostedToolConfig] = {}
    for tool, raw in payload.items():
        if tool not in MODEL_HUB_HOSTED_TOOLS:
            logger.warning("Config 'model_hub.hosted_tools' ignores an unknown tool %r", tool)
            continue
        try:
            tools[tool] = ModelHubHostedToolConfig.from_payload(raw, tool=tool)
        except ValueError as exc:
            logger.warning("%s; hosted tool %r is off", exc, tool)
            tools[tool] = ModelHubHostedToolConfig(enabled=False)
    return tools
