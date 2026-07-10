"""Platform-neutral modal data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from modules.settings_manager import ChannelRouting


@dataclass
class RoutingModalData:
    """Shared routing modal payload consumed by platform renderers."""

    registered_backends: list[str]
    current_backend: str
    current_routing: ChannelRouting | None
    opencode_agents: list[str]
    opencode_models: dict[str, list[str]]
    opencode_default_config: dict[str, Any]
    claude_agents: list[str]
    claude_models: list[str]
    codex_agents: list[str]
    codex_models: list[str]
    backend_reasoning_options: dict[str, dict[str, list[dict[str, str]]]]

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "registered_backends": self.registered_backends,
            "current_backend": self.current_backend,
            "current_routing": self.current_routing,
            "opencode_agents": self.opencode_agents,
            "opencode_models": self.opencode_models,
            "opencode_default_config": self.opencode_default_config,
            "claude_agents": self.claude_agents,
            "claude_models": self.claude_models,
            "codex_agents": self.codex_agents,
            "codex_models": self.codex_models,
            "backend_reasoning_options": self.backend_reasoning_options,
        }


@dataclass
class RoutingModalSelection:
    """Normalized routing modal selection state."""

    selected_backend: str
    selected_opencode_agent: Optional[str] = None
    selected_opencode_model: Optional[str] = None
    selected_opencode_reasoning: Optional[str] = None
    selected_claude_agent: Optional[str] = None
    selected_claude_model: Optional[str] = None
    selected_claude_reasoning: Optional[str] = None
    selected_codex_agent: Optional[str] = None
    selected_codex_model: Optional[str] = None
    selected_codex_reasoning: Optional[str] = None
