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
from storage.agent_session_rows import is_bot_session, session_openable_in_chat
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, scopes
from vibe.authorization import AuthorizationContext, require_instance_role

logger = logging.getLogger(__name__)

# Maximum ids per SQLite ``IN (...)`` chunk so pathological messages stay well
# below SQLite's bind-parameter limit.
_SQL_IN_CHUNK_SIZE = 400

# Matches fenced code blocks (closed or still-open while streaming), inline code
# spans (matching backtick run lengths, no newline inside), autolinks
# (`<https://...>`), full Markdown inline links (`[label](dest)` — so mentions
# inside an existing link label never create nested links), and bare link
# destinations (`](dest)` fallback when a label contains nested brackets).
# Everything matched here is passed through verbatim.
_SKIP_SEGMENT_RE = re.compile(
    r"(?P<fence>```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))"
    r"|(?P<code_span>(?P<bt>`+)[^`\n]+?(?P=bt))"
    r"|(?P<autolink><https?://[^>\s]+>)"
    r"|(?P<md_link>\[[^\]\n]*\]\((?:[^()\s]|(?:\([^()\s]*\)))*\))"
    r"|(?P<link_dest>\]\((?:[^()\s]|(?:\([^()\s]*\)))*\))",
)

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
class ResolvedMentions:
    """Resolved Session and Bot lookups for one batch."""

    sessions: dict[str, ResolvedSession] = field(default_factory=dict)
    bots: dict[str, ResolvedBot] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.sessions and not self.bots


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


def _iter_prose_slices(text: str) -> Iterable[tuple[int, int, bool]]:
    """Yield ``(start, end, is_prose)`` partitioning *text* around code/URLs.

    Skips fenced code blocks, inline code spans, autolinks, Markdown link
    destinations, and CommonMark indented code lines (4+ leading spaces or a
    leading tab at an actual line start).
    """

    def _split_indented_lines(seg_start: int, seg_end: int) -> Iterable[tuple[int, int, bool]]:
        cursor = seg_start
        while cursor < seg_end:
            nl = text.find("\n", cursor, seg_end)
            line_end = seg_end if nl < 0 else nl + 1
            at_line_start = cursor == 0 or text[cursor - 1] == "\n"
            is_indented = at_line_start and text.startswith(("    ", "\t"), cursor, line_end)
            yield cursor, line_end, not is_indented
            cursor = line_end

    pos = 0
    for match in _SKIP_SEGMENT_RE.finditer(text):
        if match.start() > pos:
            yield from _split_indented_lines(pos, match.start())
        yield match.start(), match.end(), False
        pos = match.end()
    if pos < len(text):
        yield from _split_indented_lines(pos, len(text))


def _might_contain_mentions(text: str, *, include_bare_sessions: bool) -> bool:
    if not text:
        return False
    if "#<" in text or "@<bot:" in text:
        return True
    return include_bare_sessions and "#ses" in text


def extract_mention_candidates(
    text: str,
    *,
    include_bare_sessions: bool = False,
) -> tuple[list[str], list[str]]:
    """Return deduplicated ``(session_ids, bot_handles)`` referenced in prose."""
    if not _might_contain_mentions(text, include_bare_sessions=include_bare_sessions):
        return [], []
    pattern = _AGENT_MENTION_RE if include_bare_sessions else _BRACKETED_MENTION_RE
    session_ids: list[str] = []
    seen_sessions: set[str] = set()
    bot_handles: list[str] = []
    seen_bots: set[str] = set()
    for start, end, is_prose in _iter_prose_slices(text):
        if not is_prose:
            continue
        segment = text[start:end]
        for match in pattern.finditer(segment):
            bare = match.groupdict().get("bare_session")
            if bare:
                if bare not in seen_sessions:
                    seen_sessions.add(bare)
                    session_ids.append(bare)
                continue
            trigger = match.group("trigger")
            inner = (match.group("inner") or "").strip()
            if not inner:
                continue
            if trigger == "#":
                if inner not in seen_sessions:
                    seen_sessions.add(inner)
                    session_ids.append(inner)
            elif trigger == "@" and inner.startswith("bot:"):
                handle = inner[4:].strip()
                if handle and handle not in seen_bots:
                    seen_bots.add(handle)
                    bot_handles.append(handle)
    return session_ids, bot_handles


def resolve_mentions_in_conn(
    conn: Connection,
    *,
    session_ids: Iterable[str] = (),
    bot_handles: Iterable[str] = (),
    authorization_context: AuthorizationContext | Mapping[str, Any] | None = None,
    language: str | None = None,
    config: V2Config | None = None,
) -> ResolvedMentions:
    """Batch-resolve openable Sessions and active Bots in one indexed query."""
    unique_sessions = list(dict.fromkeys(str(sid).strip() for sid in session_ids if str(sid or "").strip()))
    unique_bots = list(dict.fromkeys(str(h).strip() for h in bot_handles if str(h or "").strip()))
    if not unique_sessions and not unique_bots:
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

    return ResolvedMentions(sessions=resolved_sessions, bots=resolved_bots)


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
    Markdown links stay verbatim. When *escape_markdown* is True (IM delivery),
    untrusted titles and display names have Markdown metacharacters escaped.
    """
    if not text or resolved.is_empty():
        return text
    if not _might_contain_mentions(text, include_bare_sessions=include_bare_sessions):
        return text
    pattern = _AGENT_MENTION_RE if include_bare_sessions else _BRACKETED_MENTION_RE

    def _replace(match: re.Match[str]) -> str:
        bare = match.groupdict().get("bare_session")
        if bare:
            session = resolved.sessions.get(bare)
            if session is None:
                return match.group(0)
            return _format_session_mention(
                session, link_session_url, escape_markdown=escape_markdown
            )
        trigger = match.group("trigger")
        inner = (match.group("inner") or "").strip()
        if trigger == "#":
            session = resolved.sessions.get(inner)
            if session is None:
                return match.group(0)
            return _format_session_mention(
                session, link_session_url, escape_markdown=escape_markdown
            )
        if trigger == "@" and inner.startswith("bot:"):
            handle = inner[4:].strip()
            bot = resolved.bots.get(handle)
            if bot is None:
                return match.group(0)
            name = _escape_markdown_text(bot.display_name) if escape_markdown else bot.display_name
            return f"@{name}"
        return match.group(0)

    parts: list[str] = []
    for start, end, is_prose in _iter_prose_slices(text):
        segment = text[start:end]
        parts.append(pattern.sub(_replace, segment) if is_prose else segment)
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
    session_ids, bot_handles = extract_mention_candidates(text, include_bare_sessions=True)
    if not session_ids and not bot_handles:
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
    session_ids, bot_handles = extract_mention_candidates(
        text, include_bare_sessions=include_bare_sessions
    )
    if not session_ids and not bot_handles:
        return text
    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=session_ids,
        bot_handles=bot_handles,
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
    candidate_rows: list[dict[str, Any]] = []
    for row in inbox_rows:
        preview = row.get("preview_text")
        if not isinstance(preview, str) or not preview:
            continue
        if not _might_contain_mentions(preview, include_bare_sessions=True):
            continue
        sids, handles = extract_mention_candidates(preview, include_bare_sessions=True)
        if not sids and not handles:
            continue
        candidate_rows.append(row)
        all_sessions.extend(sids)
        all_bots.extend(handles)
    if not candidate_rows:
        return
    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=all_sessions,
        bot_handles=all_bots,
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

    * Agent-authored rows get resolved Session and Bot references for both
      bracketed (``#<id>``, ``@<bot:handle>``) and bare (``#ses...``) mentions.
      Unresolved or inaccessible ids are omitted so the client leaves them as
      plain text.
    * User-authored rows that already carry a composer ``references`` sidecar
      have their Session titles and Bot display names refreshed to current
      values, with ``openable`` set from live access checks.
    """
    if not rows:
        return
    all_sessions: list[str] = []
    all_bots: list[str] = []
    row_candidates: list[tuple[dict[str, Any], bool, list[str], list[str]]] = []

    for row in rows:
        author = str(row.get("author") or "").strip()
        text = row.get("text")
        content = row.get("content") if isinstance(row.get("content"), dict) else None
        existing_refs = content.get("references") if content else None
        has_existing = isinstance(existing_refs, list) and len(existing_refs) > 0

        if author == "agent":
            if not isinstance(text, str) or not text:
                continue
            sids, handles = extract_mention_candidates(text, include_bare_sessions=True)
            if not sids and not handles:
                continue
            row_candidates.append((row, True, sids, handles))
            all_sessions.extend(sids)
            all_bots.extend(handles)
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
            row_candidates.append((row, False, sids, handles))
            all_sessions.extend(sids)
            all_bots.extend(handles)

    if not row_candidates:
        return

    resolved = resolve_mentions_in_conn(
        conn,
        session_ids=all_sessions,
        bot_handles=all_bots,
        authorization_context=authorization_context,
        language=language,
    )

    for row, is_agent, sids, handles in row_candidates:
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
