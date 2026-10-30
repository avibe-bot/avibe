"""The MCP discovery index injected into the shared system prompt."""

from __future__ import annotations

import logging
from typing import Optional

from core.mcp_hub import repository
from core.mcp_hub.definition import normalize_description
from core.prompt_registry import RenderedPromptBlock, render_prompt_block

logger = logging.getLogger(__name__)

PROMPT_SERVER_LIMIT = 50


def build_mcp_prompt_block() -> Optional[RenderedPromptBlock]:
    """One line per enabled, generated package; omitted when there is none.

    The index grants nothing: every call rechecks the published revision at the owner.
    """

    try:
        packages = repository.list_packages()
    except Exception:
        logger.warning("MCP prompt index unavailable", exc_info=True)
        return None
    if not packages:
        return None
    rows = []
    for item in packages[:PROMPT_SERVER_LIMIT]:
        description = normalize_description(item.get("description"))
        rows.append(f"- {item['name']}: {description}" if description else f"- {item['name']}")
    omitted = len(packages) - PROMPT_SERVER_LIMIT
    if omitted > 0:
        rows.append(f"- … {omitted} more; run `vibe mcp --list`.")
    return render_prompt_block("mcp-capabilities-prompt", server_rows="\n".join(rows))
