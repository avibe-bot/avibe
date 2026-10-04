"""What the Avibe Agent supplies to C-9 context management (``agent-core-contracts/context.md`` sections 7 and 9).

``AvibeContextHost`` is the loop's ``ContextHost``:

* ``earlier_record`` is the lookup command a checkpoint carries in
  ``<earlier-record>``: one ``vibe data query`` over every Session whose rows the
  context holds (the Session and its fork ancestry), up to the last summarized
  ``context_seq``;
* ``render_state`` renders, from their own stores, the state a checkpoint
  carries: the environment's core fields (a checkpoint always happens inside a
  run; bounded by construction), then the bodies of the skills the summarized
  rows loaded, within the cap of the route the next request goes to.

A skill load is a ``bash`` call to ``vibe skill load``, which writes one
``<skill_content name="...">`` block per skill into the result; ``mark_skill_loads``
records those names in the result's details (``details.skills``), which is how
clearing spares a skill load and a checkpoint carries it (sections 4 and 7).
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy.engine import Engine

from core.agent_core.harness.context import PATH_CHARS, SkillRef, StateRequest, text_tokens, truncate_middle
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from modules.agents.avibe.prompt import render_environment

#: Each skill body a checkpoint carries, in tokens (section 7); the skills share the route's cap (``state_cap``).
SKILL_TOKENS = 5_000
#: Below this much of the room left, no further skill can carry anything useful.
_FLOOR = 200
#: The skills left out are named, at most this many, then "and N more".
_LISTED = 20
_NAME_CHARS = 64

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
#: The tags of the block ``vibe skill load`` writes for each skill it loads (``render_skill_content``).
_SKILL_TAG = re.compile(r'<skill_content name="([^"]*)"[^>]*>|</skill_content>')
#: What the model read of a ``messages`` row: the text of its model message, else the display text.
_MODEL_TEXT = (
    "coalesce((select group_concat(json_extract(value, '$.text'), char(10))"
    " from json_each(content_json, '$.model.message.content')), content_text)"
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


@dataclass(frozen=True)
class SkillScope:
    """Where a Session's skills resolve: its latest Turn's working directory and managed-skill bindings.

    The same inputs the Turn's ``vibe skill load`` resolves with, so a checkpoint carries the body the Turn would
    load.
    """

    cwd: Optional[str]
    project_base: Optional[str] = None
    claude_cli_path: Optional[str] = None

    def catalog(self) -> Any:
        """The resolved skill catalog, once per use: resolving scans every skill root (and may run ``claude``)."""
        from core.managed_skills import resolve_skills

        return resolve_skills(self.cwd or None, project_base=self.project_base, claude_cli_path=self.claude_cli_path)

    def load(self, name: str, catalog: Any = None) -> Any:
        """The skill with its body from ``catalog`` (resolved now when not given), or None when it does not resolve."""
        from core.managed_skills import load_skill

        return load_skill(name, self.cwd or None, resolved_skills=self.catalog() if catalog is None else catalog)


class AvibeContextHost:
    """The loop's ``ContextHost`` for the Avibe Agent."""

    def __init__(
        self,
        engine: Engine,
        *,
        environment: Callable[[str], Mapping[str, str]],
        skills: Callable[[str], Optional[SkillScope]],
    ) -> None:
        self._engine = engine
        self._environment = environment
        self._skills = skills

    def earlier_record(self, session_id: str, through_seq: int) -> Optional[str]:
        """``vibe data query`` over the inputs and replies the checkpoint summarized; the model replaces ``KEYWORD``.

        It searches what the model read (each row's model message, else its display text), from ``messages`` only,
        which every caller of ``vibe data query`` may read; tool outputs can be run again.
        """
        from storage.agent_transcript import context_members

        with self._engine.connect() as conn:
            members = context_members(conn, session_id)
        bounds = []
        for member, bound in members:
            if not _SESSION_ID.fullmatch(member):
                return None  # never quoted into a command
            limit = through_seq if bound is None else min(bound, through_seq)
            bounds.append(f"(session_id = '{member}' and context_seq <= {int(limit)})")
        sql = (
            f"select context_seq, type, text from (select context_seq, type, {_MODEL_TEXT} as text from messages"
            f" where {' or '.join(bounds)}) where text like '%KEYWORD%' order by context_seq"
        )
        return f'vibe data query --limit 100 --sql "{sql}"'

    async def render_state(self, request: StateRequest) -> list[str]:
        environment = _environment(self._environment(request.session_id))
        scope = self._skills(request.session_id)
        skills = await asyncio.to_thread(_skill_texts, scope, request.skills, request.cap - text_tokens(environment))
        return [environment, *skills]


def _environment(fields: Mapping[str, str]) -> str:
    """The environment's core fields, bounded by construction: no Watches, the cwd cut in the middle (section 7)."""
    core = {name: value for name, value in fields.items() if name != "watches"}
    if "cwd" in core:
        core["cwd"] = truncate_middle(core["cwd"], PATH_CHARS)
    return render_environment(core)


def _left_out(names: Sequence[str]) -> str:
    listed = ", ".join(name[:_NAME_CHARS] for name in names[:_LISTED])
    more = f", and {len(names) - _LISTED} more" if len(names) > _LISTED else ""
    return (
        f'<left-out of="skills">Left out to keep the carried state within its cap: {listed}{more}. Run '
        "`vibe skill load -- <name>` for any you still need.</left-out>"
    )


def _skill_texts(scope: Optional[SkillScope], skills: Sequence[SkillRef], budget: int) -> list[str]:
    """The carried skills within ``budget`` tokens, in order, each loaded only when there is room for it.

    The room for the left-out notice is held back first (the most it can take: the longest names, all counted);
    what does not fit is named, up to ``_LISTED`` names and then "and N more". One catalog per checkpoint.
    """
    if not skills:
        return []
    from core.skill_observability import skill_revision

    longest = sorted((ref.name[:_NAME_CHARS] for ref in skills), key=lambda name: len(name.encode()), reverse=True)
    room = budget - text_tokens(_left_out(longest))
    catalog: list[Any] = []
    texts: list[str] = []
    left_out: list[str] = []
    for ref in skills:
        text: Optional[str] = None
        if room >= _FLOOR:
            if scope is not None and not catalog:
                catalog.append(scope.catalog())
            skill = scope.load(ref.name, catalog[0]) if scope is not None else None
            if skill is None or skill.body is None:
                text = (
                    f'<skill-unavailable name="{_attr(ref.name)}">This skill no longer loads. Run '
                    f"`vibe skill load -- {ref.name}` if you still need it.</skill-unavailable>"
                )
            else:
                text = _skill_text(ref.name, skill_revision(skill) or "", skill.body, min(SKILL_TOKENS, room))
        if text is None or text_tokens(text) > room:
            left_out.append(ref.name)
            continue
        texts.append(text)
        room -= text_tokens(text)
    if left_out:
        texts.append(_left_out(left_out))
    return texts


def _skill_text(name: str, revision: str, body: str, limit: int) -> Optional[str]:
    """A skill body as the checkpoint carries it, cut to ``limit`` tokens; None when not even a cut fits."""
    head = f'<skill_content name="{_attr(name)}" revision="{_attr(revision)}">\n'
    tail = "</skill_content>"
    text = f"{head}{body}{tail}"
    if text_tokens(text) <= limit:
        return text
    note = f"\n[Skill body cut to fit the checkpoint. Run `vibe skill load -- {name}` for all of it.]\n"
    room = (limit - text_tokens(head + note + tail) - 1) * 4
    if room <= 0:
        return None
    kept = body.encode("utf-8")[:room].decode("utf-8", "ignore")
    return f"{head}{kept}{note}{tail}"


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


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
