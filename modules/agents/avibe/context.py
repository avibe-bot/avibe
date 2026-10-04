"""What the Avibe Agent supplies to C-9 context management (``agent-core-contracts/context.md`` sections 7 and 9).

``AvibeContextHost`` is the loop's ``ContextHost``:

* ``earlier_record`` is the lookup command a checkpoint carries in
  ``<earlier-record>``: one ``vibe data query`` over every Session whose rows the
  context holds (the Session and its fork ancestry), up to the last summarized
  ``context_seq``;
* ``render_state`` renders, from their own stores, the state a checkpoint
  carries: the full environment block (a checkpoint always happens inside a
  run), the Session's pending Watches, Tasks, and delegated Runs, and the bodies
  of the skills the summarized rows loaded, all within one budget (``_render``).

A skill load is a ``bash`` call to ``vibe skill load``; ``mark_skill_loads``
records the skill's name and revision in that call's result details, which is
how clearing spares a skill load and a checkpoint carries it (section 4, 7).
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from sqlalchemy.engine import Engine

from core.agent_core.harness.context import SkillRef, StateRequest, text_tokens
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from modules.agents.avibe.prompt import render_environment

#: Everything a checkpoint rehydrates shares one budget, and each skill body is cut to its own, in tokens (section 7).
STATE_TOKENS = 25_000
SKILL_TOKENS = 5_000
#: Below this much of the budget left, no further item can carry anything useful.
_FLOOR = 200
#: What a section leaves out is named, at most this many, then "and N more"; its wrapper and that notice are kept
#: inside the budget.
_LISTED = 20
_NAME_CHARS = 64
_NOTICE_TOKENS = 600
_LABEL_CHARS = 200

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
_SKILL_LOAD = re.compile(
    r"(?:^|[\s;&|(/])vibe\s+skill\s+load\s+(?:--\s+)?(?P<quote>['\"]?)(?P<name>[A-Za-z0-9][A-Za-z0-9._:-]*)(?P=quote)"
)


def output_budget(capabilities: Any) -> int:
    """The output the Agent asks for on a route: its own maximum, at most a quarter of the window (8,192 floor).

    Many Model Hub definitions list an output maximum as large as the window; reserving all of it would leave no room
    for the context (C-9 section 1). An unknown maximum stays 8,192.
    """
    from core.agent_core.harness.context import DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS

    maximum = capabilities.max_output_tokens
    if maximum is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    window = capabilities.context_window or DEFAULT_CONTEXT_WINDOW
    return min(maximum, max(DEFAULT_MAX_OUTPUT_TOKENS, window // 4))


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

    The same inputs the Turn's ``vibe skill load`` resolves with, so a revision recorded at load time and one
    rendered later agree while the skill is unchanged.
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

    def revision(self, name: str) -> Optional[str]:
        from core.skill_observability import skill_revision

        skill = self.load(name)
        return skill_revision(skill) if skill is not None else None


#: One item of rehydrated state: its name, and its text within a token limit (None when it cannot fit at all).
_Item = Tuple[str, Callable[[int], Optional[str]]]


@dataclass(frozen=True)
class _Section:
    """One kind of rehydrated state: its items in order, how its kept texts join (None: each is its own text), and
    the hint its left-out notice ends with."""

    name: str
    items: Sequence[_Item]
    join: Optional[Callable[[list[str]], str]]
    hint: str


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

        It reads ``messages`` only, which every caller of ``vibe data query`` may read; tool outputs can be run again.
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
            "select context_seq, type, content_text from messages"
            f" where ({' or '.join(bounds)}) and content_text like '%KEYWORD%' order by context_seq"
        )
        return f'vibe data query --limit 100 --sql "{sql}"'

    async def render_state(self, request: StateRequest) -> list[str]:
        environment = render_environment(self._environment(request.session_id))
        scope = self._skills(request.session_id)
        return await asyncio.to_thread(self._state, request, environment, scope)

    def _state(self, request: StateRequest, environment: str, scope: Optional[SkillScope]) -> list[str]:
        sections = (
            _Section("environment", (("environment", lambda limit: environment),), None, "The next input carries it."),
            _Section(
                "pending work",
                self._pending_work(request.session_id),
                lambda lines: "<pending-work>\n" + "".join(lines) + "</pending-work>",
                "Each is still pending; `vibe watch list`, `vibe task list`, and `vibe runs list` show them.",
            ),
            _Section(
                "skills",
                _skill_items(scope, request.skills),
                None,
                "Run `vibe skill load -- <name>` for any you still need.",
            ),
        )
        return _render(sections, STATE_TOKENS)

    def _pending_work(self, session_id: str) -> tuple[_Item, ...]:
        from storage.workbench_sessions_service import derive_session_harness_activities

        with self._engine.connect() as conn:
            items = derive_session_harness_activities(conn, session_id)
        rendered = []
        for item in items:
            kind = item.get("item_kind") or "item"
            label = " ".join(str(item.get("label") or "").split())[:_LABEL_CHARS]
            name = f"{kind} {str(item.get('id') or '').split(':', 1)[-1]}"
            line = f'- {name} "{label}": {item.get("status")} since {item.get("since")}\n'
            rendered.append((name, lambda limit, line=line: line))
        return tuple(rendered)


def _render(sections: Sequence[_Section], budget: int) -> list[str]:
    """The one renderer of rehydrated state: every section's texts, in order, within ``budget`` tokens in all.

    The items of every section share one room, in order; each section's wrapper and left-out notice are paid from
    what is held back for them. An item is rendered only when there is room for it; what does not fit is left out
    and named, up to ``_LISTED`` names and then "and N more".
    """
    texts: list[str] = []
    room = budget - _NOTICE_TOKENS * len(sections)
    for section in sections:
        kept: list[str] = []
        left_out: list[str] = []
        for name, render in section.items:
            text = render(room) if room >= _FLOOR else None
            if text is None or text_tokens(text) > room:
                left_out.append(name)
                continue
            kept.append(text)
            room -= text_tokens(text)
        if kept:
            texts.extend([section.join(kept)] if section.join is not None else kept)
        if left_out:
            listed = ", ".join(name[:_NAME_CHARS] for name in left_out[:_LISTED])
            more = f", and {len(left_out) - _LISTED} more" if len(left_out) > _LISTED else ""
            texts.append(
                f'<left-out of="{section.name}">Over the {STATE_TOKENS:,}-token budget for carried state: '
                f"{listed}{more}. {section.hint}</left-out>"
            )
    return texts


def _skill_items(scope: Optional[SkillScope], skills: Sequence[SkillRef]) -> tuple[_Item, ...]:
    """The carried skills as items: each loaded only when it is rendered, from one catalog per checkpoint."""
    from core.skill_observability import skill_revision

    catalog: list[Any] = []

    def item(ref: SkillRef) -> _Item:
        def render(limit: int) -> Optional[str]:
            if scope is not None and not catalog:
                catalog.append(scope.catalog())
            skill = scope.load(ref.name, catalog[0]) if scope is not None else None
            if skill is None or skill.body is None:
                return (
                    f'<skill-unavailable name="{_attr(ref.name)}">This skill no longer loads. Run '
                    f"`vibe skill load -- {ref.name}` if you still need it.</skill-unavailable>"
                )
            return _skill_text(ref.name, skill_revision(skill) or ref.revision, skill.body, min(SKILL_TOKENS, limit))

        return ref.name, render

    return tuple(item(ref) for ref in skills)


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
    """``bash`` that records a successful ``vibe skill load`` in its result details (``details.skill``)."""

    def __init__(self, tool: Tool, revision: Callable[[str], Optional[str]]) -> None:
        self._tool = tool
        self._revision = revision

    @property
    def spec(self) -> ToolSpec:
        return self._tool.spec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        result = await self._tool.execute(arguments, ctx)
        command = arguments.get("command")
        match = _SKILL_LOAD.search(command) if isinstance(command, str) else None
        if match is None or result.is_error:
            return result
        name = match.group("name")
        output = "".join(getattr(block, "text", None) or "" for block in result.content)
        if f'<skill_content name="{_attr(name)}"' not in output:
            return result  # the load did not happen
        revision = await asyncio.to_thread(self._revision, name)
        details = {**dict(result.details or {}), "skill": {"name": name, "revision": revision or ""}}
        return replace(result, details=details)


def mark_skill_loads(tools: Sequence[Tool], revision: Callable[[str], Optional[str]]) -> tuple[Tool, ...]:
    """The tools with ``bash`` recording the skills it loads (section 4: a skill load is never cleared)."""
    return tuple(_SkillLoadMarking(tool, revision) if tool.spec.name == "bash" else tool for tool in tools)
