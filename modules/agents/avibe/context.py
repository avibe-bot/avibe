"""What the Avibe Agent supplies to C-9 context management (``agent-core-contracts/context.md`` sections 7 and 9).

``AvibeContextHost`` is the loop's ``ContextHost``:

* ``earlier_record`` is the short hint a checkpoint carries in ``<earlier-record>``:
  where the earlier messages are stored and how far they go, one runnable example
  query, and the Session's fork source when it is a fork. Tool outputs are not
  there: the model runs a tool again for them;
* ``render_state`` renders the state a checkpoint carries: the environment's core
  fields (a checkpoint always happens inside a run), bounded by construction.

A skill load is a ``bash`` call to ``vibe skill load``, which writes one
``<skill_content name="...">`` block per skill into the result; ``mark_skill_loads``
records those names in the result's details (``details.skills``), which is how a
checkpoint lists it by name (section 7). The result itself clears like any other.
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy.engine import Engine

from core.agent_core.harness.context import StateRequest, display
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from modules.agents.avibe.prompt import EnvironmentValue, render_environment

#: The tags of the block ``vibe skill load`` writes for each skill it loads (``render_skill_content``).
_SKILL_TAG = re.compile(r'<skill_content name="([^"]*)"[^>]*>|</skill_content>')
#: An id the example query can quote as it is, in SQL and in the shell.
_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]+")


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

    def __init__(self, engine: Engine, *, environment: Callable[[str], Mapping[str, EnvironmentValue]]) -> None:
        self._engine = engine
        self._environment = environment

    def earlier_record(self, session_id: str, through_seq: int) -> Optional[str]:
        """Where the earlier messages are stored, with one runnable example (``example_query``).

        ``messages`` holds them and every caller may read it; tool outputs are not there, so the model runs a tool
        again for its output.
        """
        from storage.agent_transcript import fork_link

        with self._engine.connect() as conn:
            link = fork_link(conn, session_id)
        hint = (
            "The earlier messages of this conversation (your inputs and replies, through context_seq"
            f" {int(through_seq)}) are stored in Avibe's messages table, session_id = '{display(session_id)}'."
            " Tool outputs are not stored there; re-run a tool if you need its output again."
        )
        example = example_query(session_id, through_seq)
        if example is not None:
            hint += f" For example:\n{example}"
        if link is not None:
            hint += f"\nThis Session was forked from {display(link[0])} at context_seq {int(link[1])}."
        return hint

    async def render_state(self, request: StateRequest) -> list[str]:
        return [_environment(self._environment(request.session_id))]


def example_query(session_id: str, through_seq: int) -> Optional[str]:
    """One runnable ``vibe data query`` line over the Session's earlier messages, newest first; None for an id it
    could not quote as it is."""
    if _PLAIN_ID.fullmatch(session_id) is None:
        return None
    sql = (
        "SELECT context_seq, type, substr(content_text,1,500) FROM messages"
        f" WHERE session_id='{session_id}' AND context_seq <= {int(through_seq)} ORDER BY context_seq DESC LIMIT 20"
    )
    return f'vibe data query --sql "{sql}"'


def _environment(fields: Mapping[str, EnvironmentValue]) -> str:
    """The environment's core fields, bounded by construction (section 7): no Watches, and each field displayed as
    every input's block displays it (``render_environment``)."""
    return render_environment({name: value for name, value in fields.items() if name != "watches"})


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
    """The tools with ``bash`` recording the skills it loads, which a checkpoint lists by name (section 7)."""
    return tuple(_SkillLoadMarking(tool) if tool.spec.name == "bash" else tool for tool in tools)
