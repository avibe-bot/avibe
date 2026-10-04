"""What the Avibe Agent supplies to C-9 context management (``agent-core-contracts/context.md`` sections 7 and 9).

``AvibeContextHost`` is the loop's ``ContextHost``:

* ``earlier_record`` is the lookup command a checkpoint carries in
  ``<earlier-record>``: one ``vibe data query`` over the Session and its fork
  ancestry (followed in the SQL itself, up to ``_FORK_DEPTH`` forks), up to the
  last summarized ``context_seq``;
* ``render_state`` renders the state a checkpoint carries: the environment's core
  fields (a checkpoint always happens inside a run), bounded by construction.

A skill load is a ``bash`` call to ``vibe skill load``, which writes one
``<skill_content name="...">`` block per skill into the result; ``mark_skill_loads``
records those names in the result's details (``details.skills``), which is how
clearing spares a skill load and a checkpoint lists it by name (sections 4 and 7).
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional, Sequence

from core.agent_core.harness.context import ITEM_BYTES, StateRequest, truncate_middle_bytes
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from modules.agents.avibe.prompt import render_environment

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
#: The tags of the block ``vibe skill load`` writes for each skill it loads (``render_skill_content``).
_SKILL_TAG = re.compile(r'<skill_content name="([^"]*)"[^>]*>|</skill_content>')
#: How many forks up the lookup follows a Session's ancestry.
_FORK_DEPTH = 16
#: The lookup: the Session and its fork sources, each up to the least fork bound below it (``chain``), then what the
#: model read of their ``messages`` rows: each block of the model message in order, its text or the decoded string
#: values of a tool call's arguments (a path it read or wrote), else the display text. One size whatever the
#: ancestry; ``{session}`` and ``{through}`` are filled in, ``KEYWORD`` is the model's.
_LOOKUP = (
    "with recursive chain(id, bound, depth) as (select '{session}', {through}, 0"
    " union all select json_extract(s.metadata_json, '$.fork_source_session_id'),"
    " min(c.bound, json_extract(s.metadata_json, '$.fork_source_context_seq')), c.depth + 1"
    " from chain c join agent_sessions s on s.id = c.id"
    f" where c.depth < {_FORK_DEPTH} and json_extract(s.metadata_json, '$.fork_source_session_id') is not null"
    " and json_extract(s.metadata_json, '$.fork_source_context_seq') is not null)"
    " select context_seq, type, text from (select m.context_seq, m.type, coalesce((select"
    " group_concat(coalesce(json_extract(b.value, '$.text'), (select group_concat(t.atom, ' ')"
    " from json_tree(b.value, '$.arguments') t where t.type = 'text')), char(10))"
    " from json_each(m.content_json, '$.model.message.content') b), m.content_text) as text"
    " from messages m join chain c on m.session_id = c.id and m.context_seq <= c.bound)"
    " where text like '%KEYWORD%' order by context_seq"
)


def output_budget(capabilities: Any) -> int:
    """The output the Agent asks for on a route: its own maximum (8,192 when unknown), at most a quarter of the window.

    Many Model Hub definitions list an output maximum as large as the window, and a small route may list none;
    reserving all of it, or the default, would leave no room for the context (C-9 section 1).
    """
    from core.agent_core.harness.context import DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS

    maximum = capabilities.max_output_tokens
    maximum = DEFAULT_MAX_OUTPUT_TOKENS if maximum is None else maximum
    return min(maximum, (capabilities.context_window or DEFAULT_CONTEXT_WINDOW) // 4)


def budgeted(selection: Any) -> Any:
    """A resolved hop's selection with the output the Agent asks for on it (``output_budget``) as its maximum.

    Applied to every hop the router resolves, the first and each retry, so the budget always fits that hop's window.
    """
    from dataclasses import replace as _replace

    capabilities = selection.capabilities
    return _replace(selection, capabilities=_replace(capabilities, max_output_tokens=output_budget(capabilities)))


class AvibeContextHost:
    """The loop's ``ContextHost`` for the Avibe Agent."""

    def __init__(self, *, environment: Callable[[str], Mapping[str, str]]) -> None:
        self._environment = environment

    def earlier_record(self, session_id: str, through_seq: int) -> Optional[str]:
        """``vibe data query`` over the inputs and replies the checkpoint summarized; the model replaces ``KEYWORD``.

        It searches what the model read, from ``messages`` only, which every caller of ``vibe data query`` may read;
        tool outputs can be run again.
        """
        if not _SESSION_ID.fullmatch(session_id):
            return None  # never quoted into a command
        sql = _LOOKUP.replace("{session}", session_id).replace("{through}", str(int(through_seq)))
        return f'vibe data query --limit 100 --sql "{sql}"'

    async def render_state(self, request: StateRequest) -> list[str]:
        return [_environment(self._environment(request.session_id))]


def _environment(fields: Mapping[str, str]) -> str:
    """The environment's core fields, bounded by construction (section 7): no Watches, each field cut in the middle
    to ``ITEM_BYTES``."""
    return render_environment(
        {name: truncate_middle_bytes(value, ITEM_BYTES) for name, value in fields.items() if name != "watches"}
    )


class _SkillLoadMarking:
    """``bash`` that records the skills ``vibe skill load`` wrote into a successful result (``details.skills``)."""

    def __init__(self, tool: Tool) -> None:
        self._tool = tool

    @property
    def spec(self) -> ToolSpec:
        return self._tool.spec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._tool.execute(arguments, ctx)
        if result.is_error:
            return result
        # The record is the result itself: each top-level block names one skill it loaded, whatever the command
        # looked like. Blocks are balanced, so an example tag inside a skill's body is never taken for a load.
        output = "".join(getattr(block, "text", None) or "" for block in result.content)
        names: list[str] = []
        depth = 0
        for tag in _SKILL_TAG.finditer(output):
            if tag.group(1) is None:
                depth = max(0, depth - 1)
                continue
            if depth == 0:
                names.append(html.unescape(tag.group(1)))
            depth += 1
        names = list(dict.fromkeys(names))
        if not names:
            return result
        details = {**dict(result.details or {}), "skills": [{"name": name} for name in names]}
        return replace(result, details=details)


def mark_skill_loads(tools: Sequence[Tool]) -> tuple[Tool, ...]:
    """The tools with ``bash`` recording the skills it loads (section 4: a skill load is never cleared)."""
    return tuple(_SkillLoadMarking(tool) if tool.spec.name == "bash" else tool for tool in tools)
