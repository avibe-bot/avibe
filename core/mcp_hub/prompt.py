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

    from vibe.i18n import t

    try:
        packages = repository.enabled_generated()
    except Exception:
        logger.warning("MCP prompt index unavailable", exc_info=True)
        return None
    if not packages:
        return None
    rows = [
        f"- {item['name']}: {normalize_description(item.get('description')) or t('mcp.genericDescription')}"
        for item in packages[:PROMPT_SERVER_LIMIT]
    ]
    omitted = len(packages) - PROMPT_SERVER_LIMIT
    if omitted > 0:
        rows.append(f"- … {omitted} more; run `vibe mcp --list`.")
    return render_prompt_block("mcp-capabilities-prompt", server_rows="\n".join(rows))
