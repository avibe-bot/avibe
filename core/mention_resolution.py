"""Resolve Session and Bot mention markers against live SQLite state.

Shared across every read and delivery surface:

* Transcript & activity reads (``storage/messages_service.py``,
  ``vibe/ui_server.py``): batch-resolves mentioned Sessions and Bots in one
  primary-key-indexed query per page, filtered by the reader's
  ``AuthorizationContext``, and attaches fresh ``references`` so the Web /
  Desktop / iOS renderer shows clickable chips with current titles and leaves
  unresolvable ids as exact plain text.
* Notifications and Inbox previews (``storage/messages_service.py``,
  ``core/web_push_notifications.py``): renders resolved mentions as readable
  plain text (the Session's current title; ``@<display name>`` for Bots).
* IM delivery (``core/message_dispatcher.py``): renders resolved Session
  mentions as Markdown links to the instance's public Session page when the
  platform supports links and a public URL is configured, otherwise as the
  Session's title alone. Persisted rows keep their canonical markers verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from sqlalchemy import Connection, select

from config.platform_registry import get_platform_descriptor
from config.v2_config import V2Config
from core.avibe_cloud import workbench_url
from core.bots import bot_display_name, bot_handle_for_session_id, list_bots_in
from core.vibe_agents import normalize_agent_name
from storage.agent_session_rows import is_bot_session, session_openable_in_chat
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, agents, scopes
from vibe.authorization import AuthorizationContext, require_instance_role

logger = logging.getLogger(__name__)

# Maximum ids per SQLite ``IN (...)`` chunk so pathological messages stay well
# below SQLite's bind-parameter limit.
_SQL_IN_CHUNK_SIZE = 400

# Matches:
# 1. Line-start fenced code blocks (closed at a line-start closing fence, or
#    still open at end-of-text while streaming).
# 2. Closed mid-line fenced blocks (for backwards compatibility with inline
#    fences that have an explicit closing fence).
# 3. Inline code spans (matching backtick run lengths, no newline inside).
# 4. Autolinks (`<https://...>`).
# 5. Full Markdown inline links (`[label](dest)` — so mentions inside an
#    existing link label never create nested links).
# 6. Bare link destinations (`](dest)` fallback when a label contains nested
#    brackets).
_SKIP_SEGMENT_RE = re.compile(
    r"(?P<line_fence>"
    r"(?:^|\n)[ \t]*(?P<lf_bt>`{3,})[^`\n]*(?:\n(?:(?:[\s\S]*?\n)??[ \t]*(?P=lf_bt)`*[ \t\r]*(?=\n|$)|[\s\S]*$)|$)"
    r"|(?:^|\n)[ \t]*(?P<lf_tilde>~{3,})[^\n]*(?:\n(?:(?:[\s\S]*?\n)??[ \t]*(?P=lf_tilde)~*[ \t\r]*(?=\n|$)|[\s\S]*$)|$)"
    r")"
    r"|(?P<fence>(?P<f_tilde>~{3,})(?:(?!\r?\n\r?\n)[\s\S])*?(?P=f_tilde))"
    r"|(?P<code_span>(?<!`)(?P<bt>`+)(?!`)(?:(?!\r?\n\r?\n)[\s\S])*?(?<!`)(?P=bt)(?!`))"
    r"|(?P<autolink><https?://[^>\s]+>)"
    r"|(?P<md_link>(?P<md_label>\[[^\]\n]*\])(?P<md_dest>\((?:[^()\s]|(?:\([^()\s]*\)))*\)))"
    r"|(?P<link_dest>\]\((?:[^()\s]|(?:\([^()\s]*\)))*\))",
)

# Exact bracketed mention marker used as a Markdown link destination, e.g.
# `[3.2.9 checklist](#<ses...>)`, `[Product](@<bot:product>)`, or `[vibey](@<agent:vibey>)`.
# Ordinary destinations (URLs, `#heading` anchors, URL fragments) never match.
_MARKER_LINK_DEST_RE = re.compile(r"^\((?P<trigger>[#@])<(?P<inner>[^>\n]+)>\)$")

_LIST_ITEM_LINE_RE = re.compile(r"^ {0,3}(?:[-*+]|\d+[.)])[ \t]")
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]")

# Bracketed Session (`#<session-id>`) and Bot (`@<bot:handle>`) markers.
_BRACKETED_MENTION_RE = re.compile(r"(?P<trigger>[#@])<(?P<inner>[^>\n]+)>")

# Bare `#ses...` written by models that omit brackets, matched only in Agent
# replies and converted only when the candidate resolves to an openable Session.
# Allows CJK/punctuation immediately before `#` (e.g. `（#sesw34zgzgcvp）`),
# rejects URL fragments/paths (`/#ses...`, `x#ses...`), doubled hashes, and
# backslash escapes.
_BARE_SESSION_PART = (
    r"(?<![A-Za-z0-9_/:.?=&@#\\])"
    r"#(?P<bare_session>ses(?:_[a-z][a-z0-9-]{1,23}|[A-Za-z0-9][A-Za-z0-9_-]{0,62}))"
    r"(?![A-Za-z0-9_-])"
)

_AGENT_MENTION_RE = re.compile(
    rf"(?P<trigger>[#@])<(?P<inner>[^>\n]+)>|{_BARE_SESSION_PART}"
)

# Strip C0/C1 controls, line/paragraph separators, and bidi override/isolate
# characters from untrusted titles before inserting them into text or links.
_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200b\u200e\u200f\u202a-\u202e"
    r"\u2028\u2029\u2060\u2066-\u2069\ufeff]"
)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ResolvedSession:
    """One Session the reader is allowed to open."""

    session_id: str
    title: str | None
    display_title: str


@dataclass(frozen=True)
class ResolvedBot:
    """One active Bot."""

    handle: str
    display_name: str


@dataclass(frozen=True)
class ResolvedAgent:
    """One enabled, accessible Agent."""

    name: str


@dataclass(frozen=True)
class ResolvedMentions:
    """Resolved Session, Bot, and Agent lookups for one batch."""

    sessions: dict[str, ResolvedSession] = field(default_factory=dict)
    bots: dict[str, ResolvedBot] = field(default_factory=dict)
    agents: dict[str, ResolvedAgent] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.sessions and not self.bots and not self.agents


def _clean_title(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    cleaned = _CONTROL_RE.sub(" ", raw)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


_MARKDOWN_ESCAPE_RE = re.compile(r"([\\[\]*_~`<>])")


def _escape_markdown_text(value: str) -> str:
    """Escape Markdown syntax characters in untrusted titles for IM delivery.

    Every IM adapter in ``modules/im/`` honours CommonMark backslash escapes
    (natively on Discord, and via ``hold_markdown_escapes`` on Slack, Telegram,
    Feishu/Lark, and WeChat), so escaped titles render literally without turning
    user-editable Session titles into live links or formatting.
    """
    return _MARKDOWN_ESCAPE_RE.sub(r"\\\1", value)


def _configured_language(config: V2Config | None = None) -> str:
    try:
        cfg = config if config is not None else V2Config.load()
        lang = str(getattr(cfg, "language", "") or "").strip()
        return lang or "en"
    except Exception:
        return "en"


def _untitled_session_label(language: str) -> str:
    from vibe.i18n import t

    label = t("session.untitled", language)
    if not label or label == "session.untitled":
        return "未命名会话" if str(language).lower().startswith("zh") else "Untitled session"
    return label


def _has_runtime_management_access(context: AuthorizationContext) -> bool:
    return not context.is_remote or context.can_manage_instance


def _parse_marker_link_dest(dest: str) -> tuple[str, str] | None:
    """If *dest* is literally ``(#<id>)``, ``(@<bot:handle>)``, or ``(@<agent:name>)``,
    return ``(kind, value)``; otherwise return None so ordinary links stay untouched."""
    m = _MARKER_LINK_DEST_RE.match(dest)
    if m is None:
        return None
    trigger = m.group("trigger")
    inner = (m.group("inner") or "").strip()
    if not inner:
        return None
    if trigger == "#":
        return ("session", inner)
    if trigger == "@" and inner.startswith("bot:"):
        handle = inner[4:].strip()
        return ("bot", handle) if handle else None
    if trigger == "@" and inner.startswith("agent:"):
        name = inner[6:].strip()
        return ("agent", name) if name else None
    return None


def _normalize_agent_lookup(name: str) -> str | None:
    try:
        return normalize_agent_name(name)
    except ValueError:
        return None


def _iter_slices(text: str) -> Iterable[tuple[int, int, str, tuple[str, str] | None]]:
    """Yield ``(start, end, kind, marker_target)`` partitioning *text*.

    ``kind`` is ``"prose"``, ``"marker_link"`` (a non-image Markdown link outside
    code whose destination is an exact bracketed mention marker), or ``"skip"``.
    """

    def _is_blank_physical_line(line_start: int) -> bool:
        nl = text.find("\n", line_start)
        line_end = len(text) if nl < 0 else nl
        return not text[line_start:line_end].strip(" \t\r")

    after_blank = True
    in_indented_code = False
    last_non_blank_was_list = False
    current_line_is_code = False

    def _split_indented_lines(
        seg_start: int, seg_end: int
    ) -> Iterable[tuple[int, int, str, tuple[str, str] | None]]:
        nonlocal after_blank, in_indented_code, last_non_blank_was_list, current_line_is_code
        cursor = seg_start
        while cursor < seg_end:
            nl = text.find("\n", cursor, seg_end)
            line_end = seg_end if nl < 0 else nl + 1
            at_line_start = cursor == 0 or text[cursor - 1] == "\n"
            if at_line_start:
                if _is_blank_physical_line(cursor):
                    after_blank = True
                    current_line_is_code = False
                    yield cursor, line_end, "prose", None
                    cursor = line_end
                    continue
                line = text[cursor:line_end]
                if line.startswith(("    ", "\t")):
                    if _LIST_MARKER_RE.match(line.lstrip(" \t")):
                        in_indented_code = False
                        after_blank = False
                        last_non_blank_was_list = True
                        current_line_is_code = False
                    elif in_indented_code or (after_blank and not last_non_blank_was_list):
                        in_indented_code = True
                        after_blank = False
                        current_line_is_code = True
                    else:
                        in_indented_code = False
                        after_blank = False
                        current_line_is_code = False
                else:
                    in_indented_code = False
                    after_blank = False
                    last_non_blank_was_list = bool(_LIST_ITEM_LINE_RE.match(line))
                    current_line_is_code = False
            yield cursor, line_end, "skip" if current_line_is_code else "prose", None
            cursor = line_end

    pos = 0
    for match in _SKIP_SEGMENT_RE.finditer(text):
        if match.start() > pos:
            yield from _split_indented_lines(pos, match.start())
        md_dest = match.group("md_dest")
        is_image = match.start() > 0 and text[match.start() - 1] == "!"
        marker_target = (
            _parse_marker_link_dest(md_dest)
            if md_dest is not None and not is_image and not current_line_is_code
            else None
        )
        if marker_target is not None:
            yield match.start(), match.end(), "marker_link", marker_target
        else:
            yield match.start(), match.end(), "skip", None
        if match.group("line_fence") is not None:
            seg = match.group(0)
            fence_line = seg[1:] if seg.startswith("\n") else seg
            in_indented_code = False
            after_blank = False
            current_line_is_code = False
            if not fence_line.startswith((" ", "\t")):
                last_non_blank_was_list = False
        elif match.start() == 0 or text[match.start() - 1] == "\n":
            in_indented_code = False
            after_blank = False
            last_non_blank_was_list = False
            current_line_is_code = False
        pos = match.end()
    if pos < len(text):
        yield from _split_indented_lines(pos, len(text))


def _might_contain_mentions(text: str, *, include_bare_sessions: bool) -> bool:
    if not text:
        return False
    if "#<" in text or "@<bot:" in text or "@<agent:" in text:
        return True
    return include_bare_sessions and "#ses" in text


def extract_mention_candidates(
    text: str,
    *,
    include_bare_sessions: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Return deduplicated ``(session_ids, bot_handles, agent_names)`` referenced in prose."""
    if not _might_contain_mentions(text, include_bare_sessions=include_bare_sessions):
        return [], [], []
    pattern = _AGENT_MENTION_RE if include_bare_sessions else _BRACKETED_MENTION_RE
    session_ids: list[str] = []
    seen_sessions: set[str] = set()
    bot_handles: list[str] = []
    seen_bots: set[str] = set()
    agent_names: list[str] = []
    seen_agents: set[str] = set()

    def _add(kind: str, value: str) -> None:
        if not value:
            return
        if kind == "session":
            if value not in seen_sessions:
                seen_sessions.add(value)
                session_ids.append(value)
        elif kind == "bot":
            if value not in seen_bots:
                seen_bots.add(value)
                bot_handles.append(value)
        elif kind == "agent":
            if value not in seen_agents:
                seen_agents.add(value)
                agent_names.append(value)

    for start, end, kind, marker_target in _iter_slices(text):
        if kind == "marker_link" and marker_target is not None:
            _add(marker_target[0], marker_target[1])
            continue
        if kind != "prose":
            continue
        segment = text[start:end]
        for match in pattern.finditer(segment):
            bare = match.groupdict().get("bare_session")
            if bare:
                _add("session", bare)
                continue
            trigger = match.group("trigger")
            inner = (match.group("inner") or "").strip()
            if not inner:
                continue
            if trigger == "#":
                _add("session", inner)
            elif trigger == "@" and inner.startswith("bot:"):
                _add("bot", inner[4:].strip())
            elif trigger == "@" and inner.startswith("agent:"):
                _add("agent", inner[6:].strip())
    return session_ids, bot_handles, agent_names


def resolve_mentions_in_conn(
    conn: Connection,
    *,
    session_ids: Iterable[str] = (),
    bot_handles: Iterable[str] = (),
    agent_names: Iterable[str] = (),
    authorization_context: AuthorizationContext | Mapping[str, Any] | None = None,
    language: str | None = None,
    config: V2Config | None = None,
) -> ResolvedMentions:
    """Batch-resolve openable Sessions, active Bots, and enabled Agents."""
    unique_sessions = list(dict.fromkeys(str(sid).strip() for sid in session_ids if str(sid or "").strip()))
    unique_bots = list(dict.fromkeys(str(h).strip() for h in bot_handles if str(h or "").strip()))
    unique_agents = list(dict.fromkeys(str(a).strip() for a in agent_names if str(a or "").strip()))
    if not unique_sessions and not unique_bots and not unique_agents:
        return ResolvedMentions()

    ctx: AuthorizationContext | None = None
    if authorization_context is not None:
        try:
            ctx = require_instance_role(authorization_context, "viewer")
        except PermissionError:
            return ResolvedMentions()

    resolved_language: str | None = language

    def _lang() -> str:
        nonlocal resolved_language
        if resolved_language is None:
            resolved_language = _configured_language(config)
        return resolved_language

    # Load active Bots once if any Bot mention or Bot Session id is present.
    need_bots = bool(unique_bots) or any(bot_handle_for_session_id(sid) is not None for sid in unique_sessions)
    bots_by_handle: dict[str, Any] = {}
    if need_bots and (ctx is None or ctx.is_instance_owner):
        try:
            for bot in list_bots_in(conn):
                bots_by_handle[bot.handle] = bot
        except Exception:
            logger.debug("mention_resolution: failed to load bots", exc_info=True)

    resolved_bots: dict[str, ResolvedBot] = {}
    for handle in unique_bots:
        bot = bots_by_handle.get(handle)
        if bot is None:
            continue
        display = _clean_title(bot.display_name)
        if not display:
            display = _clean_title(bot_display_name(bot.agent, _lang())) or handle
        resolved_bots[handle] = ResolvedBot(handle=handle, display_name=display)

    resolved_sessions: dict[str, ResolvedSession] = {}
    if unique_sessions:
        from storage import project_access_service

        can_manage = ctx is None or _has_runtime_management_access(ctx)
        is_owner = ctx is None or ctx.is_instance_owner
        project_read_cache: dict[str, bool] = {}

        for offset in range(0, len(unique_sessions), _SQL_IN_CHUNK_SIZE):
            chunk = unique_sessions[offset : offset + _SQL_IN_CHUNK_SIZE]
            rows = conn.execute(
                select(
                    agent_sessions.c.id,
                    agent_sessions.c.title,
                    agent_sessions.c.scope_id,
                    scopes.c.native_type.label("scope_native_type"),
                )
                .select_from(
                    agent_sessions.outerjoin(scopes, scopes.c.id == agent_sessions.c.scope_id)
                )
                .where(agent_sessions.c.id.in_(chunk))
            ).mappings().all()

            for row in rows:
                sid = str(row["id"])
                if not session_openable_in_chat(
                    session_id=sid,
                    scope_native_type=row["scope_native_type"],
                ):
                    continue
                bot_handle = bot_handle_for_session_id(sid)
                if not is_owner and (bot_handle is not None or is_bot_session(conn, sid)):
                    continue
                if not can_manage and ctx is not None:
                    project_id = project_access_service.project_id_from_scope_id(row["scope_id"])
                    if project_id is None:
                        continue
                    allowed = project_read_cache.get(project_id)
                    if allowed is None:
                        allowed = project_access_service.can_read_project(conn, ctx, project_id)
                        project_read_cache[project_id] = allowed
                    if not allowed:
                        continue

                title = _clean_title(row["title"])
                if not title and bot_handle is not None:
                    bot = bots_by_handle.get(bot_handle)
                    if bot is not None:
                        title = _clean_title(bot.display_name) or _clean_title(
                            bot_display_name(bot.agent, _lang())
                        )
                display_title = title or _untitled_session_label(_lang())
                resolved_sessions[sid] = ResolvedSession(
                    session_id=sid,
                    title=title or None,
                    display_title=display_title,
                )

    resolved_agents: dict[str, ResolvedAgent] = {}
    if unique_agents:
        normalized_by_input: dict[str, str] = {}
        for raw_name in unique_agents:
            norm = _normalize_agent_lookup(raw_name)
            if norm:
                normalized_by_input[raw_name] = norm
        unique_norms = list(dict.fromkeys(normalized_by_input.values()))
        if unique_norms:
            matched_rows: list[dict[str, Any]] = []
            for offset in range(0, len(unique_norms), _SQL_IN_CHUNK_SIZE):
                chunk = unique_norms[offset : offset + _SQL_IN_CHUNK_SIZE]
                rows = conn.execute(
                    select(agents.c.id, agents.c.name, agents.c.normalized_name)
                    .where(
                        agents.c.normalized_name.in_(chunk),
                        agents.c.enabled == 1,
                        agents.c.archived_at.is_(None),
                    )
                ).mappings().all()
                matched_rows.extend(dict(r) for r in rows)
            if ctx is not None and matched_rows:
                from storage import resource_access_service

                matched_rows = resource_access_service.filter_accessible_resources(
                    ctx,
                    "agent",
                    matched_rows,
                    connection=conn,
                )
            for row in matched_rows:
                canonical = _clean_title(row["name"]) or str(row["normalized_name"])
                norm = str(row["normalized_name"])
                resolved_agents[norm] = ResolvedAgent(name=canonical)

    return ResolvedMentions(
        sessions=resolved_sessions,
        bots=resolved_bots,
        agents=resolved_agents,
    )


def render_mentions_as_text(
    text: str,
    resolved: ResolvedMentions,
    *,
    include_bare_sessions: bool = True,
    link_session_url: Callable[[str], str | None] | None = None,
    escape_markdown: bool = False,
) -> str:
    """Replace resolved mention markers in *text* with readable titles or links.

    Unresolved markers, code spans, fenced/indented code blocks, autolinks, and
    ordinary Markdown links stay verbatim. A Markdown link whose destination is
    literally a resolved mention marker (``[label](#<id>)``) is rendered as that
    mention so it never becomes a dead link. When *escape_markdown* is True (IM
    delivery), untrusted titles and display names have Markdown metacharacters
    escaped.
    """
    if not text or resolved.is_empty():
        return text
    if not _might_contain_mentions(text, include_bare_sessions=include_bare_sessions):
        return text
    pattern = _AGENT_MENTION_RE if include_bare_sessions else _BRACKETED_MENTION_RE

    def _format_target(kind: str, value: str) -> str | None:
        if kind == "session":
            session = resolved.sessions.get(value)
            if session is None:
                return None
            return _format_session_mention(
                session, link_session_url, escape_markdown=escape_markdown
            )
        if kind == "bot":
            bot = resolved.bots.get(value)
            if bot is None:
                return None
            name = _escape_markdown_text(bot.display_name) if escape_markdown else bot.display_name
            return f"@{name}"
        if kind == "agent":
            norm = _normalize_agent_lookup(value)
            agent = resolved.agents.get(norm) if norm else None
            if agent is None:
                return None
            name = _escape_markdown_text(agent.name) if escape_markdown else agent.name
            return f"@{name}"
        return None

    def _replace(match: re.Match[str]) -> str:
        bare = match.groupdict().get("bare_session")
        if bare:
            formatted = _format_target("session", bare)
            return formatted if formatted is not None else match.group(0)
        trigger = match.group("trigger")
        inner = (match.group("inner") or "").strip()
        if trigger == "#":
            formatted = _format_target("session", inner)
            return formatted if formatted is not None else match.group(0)
        if trigger == "@" and inner.startswith("bot:"):
            formatted = _format_target("bot", inner[4:].strip())
            return formatted if formatted is not None else match.group(0)
        if trigger == "@" and inner.startswith("agent:"):
            formatted = _format_target("agent", inner[6:].strip())
            return formatted if formatted is not None else match.group(0)
        return match.group(0)

    parts: list[str] = []
    for start, end, kind, marker_target in _iter_slices(text):
        segment = text[start:end]
        if kind == "prose":
            parts.append(pattern.sub(_replace, segment))
        elif kind == "marker_link" and marker_target is not None:
            formatted = _format_target(marker_target[0], marker_target[1])
            parts.append(formatted if formatted is not None else segment)
        else:
            parts.append(segment)
    return "".join(parts)


def _format_session_mention(
    session: ResolvedSession,
    link_session_url: Callable[[str], str | None] | None,
    *,
    escape_markdown: bool = False,
) -> str:
    url = link_session_url(session.session_id) if link_session_url is not None else None
    if url:
        return f"[{_escape_markdown_text(session.display_title)}]({url})"
    if escape_markdown:
        return _escape_markdown_text(session.display_title)
    return session.display_title


def _session_public_url(session_id: str, config: V2Config | None) -> str | None:
    return workbench_url(f"/chat/{quote(session_id, safe='')}", config=config)


def render_im_mentions(
    text: str,
    *,
    platform: str,
    config: V2Config | None = None,
    db_path: Path | None = None,
    language: str | None = None,
) -> str:
    """Resolve Session and Bot mentions for outbound IM delivery.

    Links each resolved Session to its public Workbench page when *platform*
    renders masked Markdown links and the instance has an Avibe Cloud public
    URL; otherwise writes the Session's current title alone. Unresolvable ids
    remain untouched.
    """
    if not text or not _might_contain_mentions(text, include_bare_sessions=True):
        return text
    session_ids, bot_handles, agent_names = extract_mention_candidates(
        text, include_bare_sessions=True
    )
    if not session_ids and not bot_handles and not agent_names:
        return text

    try:
        descriptor = get_platform_descriptor(platform)
        can_link = bool(descriptor.capabilities.supports_markdown_links)
    except Exception:
        can_link = platform not in {"wechat"}

    resolved_config = config
    if can_link and resolved_config is None:
        try:
            resolved_config = V2Config.load()
        except Exception:
            resolved_config = None

    engine = create_sqlite_engine(db_path)
    try:
        with engine.connect() as conn:
            resolved = resolve_mentions_in_conn(
                conn,
                session_ids=session_ids,
                bot_handles=bot_handles,
                agent_names=agent_names,
                authorization_context=None,
                language=language,
                config=resolved_config,
            )
    except Exception:
        logger.debug("mention_resolution: IM lookup failed", exc_info=True)
        return text
    finally:
        engine.dispose()

    if resolved.is_empty():
        return text

    link_fn = (
        (lambda sid: _session_public_url(sid, resolved_config))
        if can_link
        else None
    )
    return render_mentions_as_text(
        text,
        resolved,
        include_bare_sessions=True,
        link_session_url=link_fn,
        escape_markdown=True,
    )


def render_plain_mentions_in_conn(
    conn: Connection,
    text: str,
    *,
    include_bare_sessions: bool = True,
    authorization_context: AuthorizationContext | Mapping[str, Any] | None = None,
    language: str | None = None,
    config: V2Config | None = None,
) -> str:
    """Resolve mentions in one preview/notification string to plain text."""
    if not text or not _might_contain_mentions(text, include_bare_sessions=include_bare_sessions):
        return text
    session_ids, bot_handles, agent_names = extract_mention_candidates(
        text, include_bare_sessions=include_bare_sessions
    )
    if not session_ids and not bot_handles and not agent_names:
        return text
    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=session_ids,
        bot_handles=bot_handles,
        agent_names=agent_names,
        authorization_context=authorization_context,
        language=language,
        config=config,
    )
    return render_mentions_as_text(
        text,
        resolved,
        include_bare_sessions=include_bare_sessions,
        link_session_url=None,
    )


def enrich_inbox_previews_in_conn(
    conn: Connection,
    inbox_rows: Sequence[dict[str, Any]],
    *,
    authorization_context: AuthorizationContext | Mapping[str, Any] | None = None,
    language: str | None = None,
) -> None:
    """Batch-replace resolved mentions in ``preview_text`` on Inbox rows in place."""
    if not inbox_rows:
        return
    all_sessions: list[str] = []
    all_bots: list[str] = []
    all_agents: list[str] = []
    candidate_rows: list[dict[str, Any]] = []
    for row in inbox_rows:
        preview = row.get("preview_text")
        if not isinstance(preview, str) or not preview:
            continue
        if not _might_contain_mentions(preview, include_bare_sessions=True):
            continue
        sids, handles, agents_list = extract_mention_candidates(
            preview, include_bare_sessions=True
        )
        if not sids and not handles and not agents_list:
            continue
        candidate_rows.append(row)
        all_sessions.extend(sids)
        all_bots.extend(handles)
        all_agents.extend(agents_list)
    if not candidate_rows:
        return
    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=all_sessions,
        bot_handles=all_bots,
        agent_names=all_agents,
        authorization_context=authorization_context,
        language=language,
    )
    if resolved.is_empty():
        return
    for row in candidate_rows:
        row["preview_text"] = render_mentions_as_text(
            row["preview_text"],
            resolved,
            include_bare_sessions=True,
            link_session_url=None,
        )


def enrich_message_rows_in_conn(
    conn: Connection,
    rows: Sequence[dict[str, Any]],
    *,
    authorization_context: AuthorizationContext | Mapping[str, Any] | None = None,
    language: str | None = None,
) -> None:
    """Attach/refresh ``content.references`` on serialized message rows in place.

    Runs ONE batch SQLite lookup across the whole page of rows, scoped to the
    caller's ``authorization_context``:

    * Agent- and Harness-authored rows get resolved Session, Bot, and Agent
      references for bracketed (``#<id>``, ``@<bot:handle>``, ``@<agent:name>``),
      marker-destination link (``[label](#<id>)``), and bare (``#ses...``)
      mentions. Unresolved or inaccessible ids are omitted so the client leaves
      them as plain text.
    * User-authored rows that already carry a composer ``references`` sidecar
      have their Session titles and Bot display names refreshed to current
      values, with ``openable`` set from live access checks.
    """
    if not rows:
        return
    all_sessions: list[str] = []
    all_bots: list[str] = []
    all_agents: list[str] = []
    row_candidates: list[tuple[dict[str, Any], bool, list[str], list[str], list[str]]] = []

    for row in rows:
        author = str(row.get("author") or "").strip()
        text = row.get("text")
        content = row.get("content") if isinstance(row.get("content"), dict) else None
        existing_refs = content.get("references") if content else None
        has_existing = isinstance(existing_refs, list) and len(existing_refs) > 0

        if author in {"agent", "harness"}:
            if not isinstance(text, str) or not text:
                continue
            sids, handles, agents_list = extract_mention_candidates(
                text, include_bare_sessions=True
            )
            if not sids and not handles and not agents_list:
                continue
            row_candidates.append((row, True, sids, handles, agents_list))
            all_sessions.extend(sids)
            all_bots.extend(handles)
            all_agents.extend(agents_list)
        elif has_existing:
            sids = [
                str(ref.get("session_id")).strip()
                for ref in existing_refs
                if isinstance(ref, dict) and ref.get("kind") == "session" and ref.get("session_id")
            ]
            handles = [
                str(ref.get("handle")).strip()
                for ref in existing_refs
                if isinstance(ref, dict) and ref.get("kind") == "bot" and ref.get("handle")
            ]
            if not sids and not handles:
                continue
            row_candidates.append((row, False, sids, handles, []))
            all_sessions.extend(sids)
            all_bots.extend(handles)

    if not row_candidates:
        return

    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=all_sessions,
        bot_handles=all_bots,
        agent_names=all_agents,
        authorization_context=authorization_context,
        language=language,
    )

    for row, is_agent, sids, handles, agents_list in row_candidates:
        content = dict(row.get("content")) if isinstance(row.get("content"), dict) else {}
        if is_agent:
            refs: list[dict[str, Any]] = []
            for sid in sids:
                session = resolved.sessions.get(sid)
                if session is None:
                    continue
                refs.append(
                    {
                        "kind": "session",
                        "session_id": session.session_id,
                        "title": session.title,
                        "openable": True,
                    }
                )
            for handle in handles:
                bot = resolved.bots.get(handle)
                if bot is None:
                    continue
                refs.append(
                    {
                        "kind": "bot",
                        "handle": bot.handle,
                        "display_name": bot.display_name,
                    }
                )
            seen_agent_names: set[str] = set()
            for raw_agent in agents_list:
                norm = _normalize_agent_lookup(raw_agent)
                agent = resolved.agents.get(norm) if norm else None
                if agent is None or agent.name in seen_agent_names:
                    continue
                seen_agent_names.add(agent.name)
                refs.append({"kind": "agent", "name": agent.name})
            if refs:
                content["references"] = refs
                row["content"] = content
        else:
            existing_refs = content.get("references")
            if not isinstance(existing_refs, list):
                continue
            updated_refs: list[Any] = []
            changed = False
            for ref in existing_refs:
                if not isinstance(ref, dict):
                    updated_refs.append(ref)
                    continue
                kind = ref.get("kind")
                if kind == "session" and ref.get("session_id"):
                    sid = str(ref["session_id"]).strip()
                    session = resolved.sessions.get(sid)
                    if session is not None:
                        updated = {**ref, "title": session.title, "openable": True}
                    else:
                        updated = {**ref, "openable": False}
                    updated_refs.append(updated)
                    changed = True
                elif kind == "bot" and ref.get("handle"):
                    handle = str(ref["handle"]).strip()
                    bot = resolved.bots.get(handle)
                    if bot is not None:
                        updated_refs.append({**ref, "display_name": bot.display_name})
                        changed = True
                    else:
                        updated_refs.append(ref)
                else:
                    updated_refs.append(ref)
            if changed:
                content["references"] = updated_refs
                row["content"] = content
