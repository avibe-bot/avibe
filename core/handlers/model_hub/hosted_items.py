"""The providers' own hosted-tool items, by catalog tool (``vibey-hosted-tools.md`` §3.2).

One vocabulary for every gateway reader of a provider's hosted items: request
admission, the executor's result reader, and the served-response observer.
"""

from __future__ import annotations

from typing import Final

# xAI runs X search as custom tool calls; their names are the provider's.
X_SEARCH_CALLS: Final = frozenset({"x_keyword_search", "x_semantic_search", "x_user_search", "x_thread_fetch"})
# Responses items that carry a hosted call's native result, by catalog tool.
RESPONSES_HOSTED_ITEMS: Final = {"web_search_call": "web_search", "image_generation_call": "image_generation"}


def responses_item_tool(kind: str | None, name: str | None) -> str | None:
    """The catalog tool a Responses output item belongs to, or None for any other item."""

    if kind == "custom_tool_call":
        return "x_search" if name in X_SEARCH_CALLS else None
    return RESPONSES_HOSTED_ITEMS.get(kind) if kind is not None else None


def responses_item_succeeded(kind: str | None, name: str | None, status: str | None) -> str | None:
    """The search tool a completed Responses item proves ran, or None.

    A ``web_search_call`` counts only when it says it completed; an X search
    call is the provider's record of a search it ran. Images are not searches.
    """

    tool = responses_item_tool(kind, name)
    if tool == "web_search":
        return tool if status == "completed" else None
    return tool if tool == "x_search" else None
