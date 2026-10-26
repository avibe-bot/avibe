"""What Vibey supplies to C-9 context management (``agent-core-contracts/context.md`` sections 7 and 9).

``VibeyContextHost`` is the loop's ``ContextHost``:

* ``earlier_record`` is the short hint a checkpoint carries in ``<earlier-record>``:
  where the earlier messages are stored and how far they go, one runnable example
  query, and the Session's fork source when it is a fork. Tool outputs are not
  there: the model runs a tool again for them;
* ``render_state`` renders the state a checkpoint carries: the environment's core
  fields (a checkpoint always happens inside a run), bounded by construction;
* ``user_messages`` says which inputs a person typed, and what: the memo's candidates
  are only those (section 7), never a Watch's, a Task's, a callback's, or another
  Session's message, whatever its text says;
* ``skill_contents`` renders skills as ``vibe skill load`` would print them in the
  Session's working directory now, which a checkpoint carries back (section 7).

A skill load is a ``bash`` call to ``vibe skill load``, which writes one
``<skill_content name="...">`` block per skill into the result; ``mark_skill_loads``
records those names in the result's details (``details.skills``), which is how a
checkpoint lists it by name (section 7). The result itself clears like any other.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from core.agent_core.harness.context import COMMAND_TOOLS, StateRequest, display
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from core.managed_skills import is_skill_name
from modules.agents.vibey.prompt import EnvironmentValue, render_environment
from storage.models import messages

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


class VibeyContextHost:
    """The loop's ``ContextHost`` for Vibey."""

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
            " Tool outputs are not stored there; re-run a tool if you need its output again. When you are unsure of"
            " the mission, of what the user said, or of the next step, read the user's own messages first, in order;"
            " a memo entry's #number is its context_seq."
        )
        example = example_query(session_id, through_seq)
        if example is not None:
            hint += f" For example:\n{example}"
        if link is not None:
            hint += f"\nThis Session was forked from {display(link[0])} at context_seq {int(link[1])}."
        return hint

    async def render_state(self, request: StateRequest) -> list[str]:
        return [_environment(self._environment(request.session_id))]

    def user_messages(self, row_ids: Sequence[str]) -> dict[str, str]:
        """The text a person typed for each of ``row_ids`` that is a person's message (``messages.type = 'user'``);
        every other input type (a Watch's, a Task's, a callback's, another Session's) is absent."""
        if not row_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(messages.c.id, messages.c.content_text).where(
                    messages.c.id.in_(list(row_ids)), messages.c.type == "user"
                )
            ).all()
        return {row.id: row.content_text or "" for row in rows}

    def skill_contents(self, session_id: str, names: Sequence[str]) -> dict[str, tuple[str, str]]:
        """Each of ``names`` as ``vibe skill load`` would print it in the Session's working directory now, with its
        revision (the SHA-256 of that text); one that no longer resolves is absent. The catalog is resolved once."""
        from core.managed_skills import load_skill, render_skill_content, resolve_skills

        cwd = str(self._environment(session_id).get("cwd") or "") or None
        catalog = resolve_skills(cwd)
        found: dict[str, tuple[str, str]] = {}
        for name in names:
            skill = load_skill(name, cwd, resolved_skills=catalog)
            if skill is not None:
                content = render_skill_content(skill)
                found[name] = (hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest(), content)
        return found


def example_query(session_id: str, through_seq: int) -> Optional[str]:
    """One runnable ``vibe data query`` line over the user's own earlier messages, in order; None for an id it could
    not quote as it is."""
    if _PLAIN_ID.fullmatch(session_id) is None:
        return None
    sql = (
        "SELECT context_seq, created_at, substr(content_text,1,300) FROM messages"
        f" WHERE session_id='{session_id}' AND type='user' AND context_seq <= {int(through_seq)}"
        " ORDER BY context_seq LIMIT 50"
    )
    return f'vibe data query --sql "{sql}" --format text'


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
            # Only a name `vibe skill load` can load: any command's output can carry a tag like it.
            if depth == 0 and is_skill_name(name := html.unescape(tag.group(1))):
                names.append(name)
            depth += 1
        names = list(dict.fromkeys(names))
        if not names:
            return result
        details = {**dict(result.details or {}), "skills": [{"name": name} for name in names]}
        return replace(result, details=details)


def mark_skill_loads(tools: Sequence[Tool]) -> tuple[Tool, ...]:
    """The tools with the command tool (``bash``, ``powershell`` on Windows) recording the skills it loads, which a
    checkpoint lists by name (section 7)."""
    return tuple(_SkillLoadMarking(tool) if tool.spec.name in COMMAND_TOOLS else tool for tool in tools)
