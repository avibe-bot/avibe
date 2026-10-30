"""The MCP discovery index injected into the shared system prompt."""

from __future__ import annotations

import logging

from core.managed_skills import catalog_page_of
from core.mcp_hub import repository
from core.mcp_hub.definition import normalize_description
from core.prompt_registry import RenderedPromptBlock, render_prompt_block

logger = logging.getLogger(__name__)

def server_row(item: dict) -> str:
    """``- <name>: <description>``, or ``- <name>`` when the server reported none."""

    description = normalize_description(item.get("description"))
    return f"- {item['name']}: {description}" if description else f"- {item['name']}"


def build_mcp_prompt_blocks() -> list[RenderedPromptBlock]:
    """Page 1 of enabled, generated packages, paged like the Skills catalog; empty when there is none.

    The index grants nothing: every call rechecks the published revision at the owner.
    """

    try:
        packages = repository.list_packages()
    except Exception:
        logger.warning("MCP prompt index unavailable", exc_info=True)
        return []
    entries, next_page = catalog_page_of([item for item in packages if item.get("auth") != "needs_login"], server_row, 1)
    if not entries:
        return []
    blocks = [render_prompt_block("mcp-capabilities-prompt")]
    if next_page is not None:
        blocks.append(render_prompt_block("mcp-pagination-prompt"))
    blocks.append(render_prompt_block("mcp-catalog", server_rows="\n".join(server_row(item) for item in entries)))
    if next_page is not None:
        notice = render_prompt_block("mcp-more-notice", next_page=next_page)
        blocks.append(RenderedPromptBlock(notice.module_id, "\n" + notice.text))
    return blocks
