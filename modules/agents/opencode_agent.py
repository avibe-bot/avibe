"""Backward-compatible import shim for OpenCode agent.

The OpenCode agent implementation was refactored into the
`modules/agents/opencode/` package.

Keep this module to preserve the public import path:

    from modules.agents.opencode_agent import OpenCodeAgent
"""

from modules.agents.opencode import (
    OpenCodeAgent,
    build_codex_reasoning_options,
    build_reasoning_effort_options,
)

__all__ = [
    "OpenCodeAgent",
    "build_codex_reasoning_options",
    "build_reasoning_effort_options",
]
