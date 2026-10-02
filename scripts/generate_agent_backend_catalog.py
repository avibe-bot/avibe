"""Project the Python backend declaration into the frontend's typed catalog."""

from __future__ import annotations

import json

from modules.agents.catalog import agent_backend_catalog_payload


def render() -> str:
    return (
        "// Generated from modules/agents/catalog.py; do not edit.\n"
        "// Regenerate with: uv run python -m scripts.generate_agent_backend_catalog\n"
        "export const BACKEND_CATALOG = "
        + json.dumps(agent_backend_catalog_payload(), ensure_ascii=False, indent=2)
        + " as const;\n"
    )


if __name__ == "__main__":
    print(render(), end="")
