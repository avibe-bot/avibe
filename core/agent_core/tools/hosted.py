"""The canonical hosted tools (``vibey-hosted-tools.md`` §2).

One user-facing tool per capability, with one name and schema in either execution mode. The schema is the
providers' trained interface: what their models call the hosted tool with. A request carries these specs like any
other tool; on a hop that runs the tool natively, the adapter sends the provider's declaration in the spec's place
(``ModelEndpoint.hosted_tools``), so ``ModelRequest.tools`` stays the one owner of what a request declares.
"""

from __future__ import annotations

import copy
from typing import Any

from core.agent_core.messages import HOSTED_TOOLS
from core.agent_core.tools.base import ToolSpec

_SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "web_search": (
        "Search the web. Returns numbered results with their titles, links and short excerpts.",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "x_search": (
        "Search posts on X. The query accepts X's search operators, such as from:user and within_time:7d. "
        "Returns numbered posts with their links and excerpts.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "minimum": 1, "description": "The most posts to return."},
                "mode": {"type": "string", "enum": ["Latest", "Top"], "description": "Newest or top posts first."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
}


def hosted_tool_spec(tool: str) -> ToolSpec:
    """The canonical spec of the search tool ``tool`` (``web_search`` or ``x_search``)."""

    if tool not in HOSTED_TOOLS:
        raise ValueError(f"unknown hosted tool: {tool!r}")
    description, schema = _SPECS[tool]
    return ToolSpec(tool, description, copy.deepcopy(schema))
