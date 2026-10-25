"""Frozen reader boundary from config.v2_config at b83d62de60f23e0894f321348400a1b9207f0011 (gh-v3.2.3rc4).

Exact source fragments, not a simulated old reader. Unchanged supporting types,
validation and I/O helpers come from the current module; the released vocabulary,
projection and identity functions below own the downgrade behavior under test.
Do not update this fixture when current readers change.
"""
from __future__ import annotations

from typing import Any, Mapping
from config.v2_config import (
    MODEL_HUB_SEARCH_TOOLS, ModelHubHostedToolConfig, ModelHubSourceConfig, logger,
)

MODEL_HUB_HOSTED_TOOLS: tuple[str, ...] = (*MODEL_HUB_SEARCH_TOOLS, "image_generate")


def _hosted_tools_from_payload(
    section: Mapping[str, Any], sources: list[ModelHubSourceConfig]
) -> dict[str, ModelHubHostedToolConfig]:
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
            tools[tool] = ModelHubHostedToolConfig.from_payload(raw, tool=tool, sources=sources)
        except ValueError as exc:
            logger.warning("%s; hosted tool %r is off", exc, tool)
            tools[tool] = ModelHubHostedToolConfig(enabled=False)
    return tools
