"""OpenCode agent backend.

This package contains the refactored OpenCode agent implementation split into
smaller modules.

Utility helpers are imported eagerly so lightweight callers can use them
without pulling in the server/runtime dependency chain. Agent/server objects
are resolved lazily via ``__getattr__``.
"""

from .utils import (
    build_claude_reasoning_options,
    build_codex_reasoning_options,
    build_reasoning_effort_options,
    resolve_model_reasoning_options,
)

__all__ = [
    "OpenCodeAgent",
    "OpenCodeModelHubOverlayRequiredError",
    "OpenCodeServerManager",
    "build_claude_reasoning_options",
    "build_codex_reasoning_options",
    "build_reasoning_effort_options",
    "project_opencode_model_hub_models",
    "resolve_model_reasoning_options",
]


def __getattr__(name: str):
    if name == "OpenCodeAgent":
        from .agent import OpenCodeAgent

        return OpenCodeAgent
    if name in {
        "OpenCodeModelHubOverlayRequiredError",
        "OpenCodeServerManager",
        "project_opencode_model_hub_models",
    }:
        from .server import (
            OpenCodeModelHubOverlayRequiredError,
            OpenCodeServerManager,
            project_opencode_model_hub_models,
        )

        return {
            "OpenCodeModelHubOverlayRequiredError": OpenCodeModelHubOverlayRequiredError,
            "OpenCodeServerManager": OpenCodeServerManager,
            "project_opencode_model_hub_models": project_opencode_model_hub_models,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
