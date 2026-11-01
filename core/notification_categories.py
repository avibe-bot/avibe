"""Shared Inbox notification categorization.

The Bot registry is the only source of truth for whether a permanent Session is
currently a Bot conversation.  In particular, a Project scope never implies a
category: Bot Sessions deliberately have no scope, and ordinary scope-less
Sessions remain ordinary Sessions.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from sqlalchemy import case, exists, func, literal, select
from sqlalchemy.engine import Connection

CATEGORY_BOT = "bot"
CATEGORY_SESSION = "session"
INBOX_CATEGORIES = (CATEGORY_BOT, CATEGORY_SESSION)


def normalize_category(value: object) -> str | None:
    """Validate an optional Inbox category filter."""

    if value is None or value == "":
        return None
    category = str(value).strip().lower()
    if category not in INBOX_CATEGORIES:
        raise ValueError("category must be one of: bot, session")
    return category


def current_bot_session_ids(conn: Connection) -> frozenset[str]:
    """Return permanent Session ids of current (not removed) Bots."""

    from core.bots import list_bots_in

    return frozenset(bot.session_id for bot in list_bots_in(conn) if bot.agent.enabled)


def current_bot_session_predicate(session_id_column):
    """Return a SQL predicate for a current Bot permanent Session.

    Keeping this expression here lets the Inbox query classify and filter in the
    same bounded statement; callers do not need a second registry read per row.
    """

    from storage.models import agents

    handle = func.json_extract(agents.c.metadata_json, "$.bot.handle")
    removed_at = func.json_extract(agents.c.metadata_json, "$.bot.removed_at")
    return exists(
        select(1)
        .select_from(agents)
        .where(
            agents.c.archived_at.is_(None),
            agents.c.enabled == 1,
            handle.is_not(None),
            removed_at.is_(None),
            session_id_column == literal("ses_") + handle,
        )
    )


def category_expression(session_id_column):
    """Return the SQL category expression backed by the current Bot registry."""

    return case(
        (current_bot_session_predicate(session_id_column), CATEGORY_BOT),
        else_=CATEGORY_SESSION,
    )


def category_for_session_id(
    session_id: object,
    *,
    bot_session_ids: Iterable[str],
) -> str:
    """Classify one Inbox destination from the Bot registry snapshot."""

    bot_ids = bot_session_ids if isinstance(bot_session_ids, (set, frozenset)) else set(bot_session_ids)
    return CATEGORY_BOT if session_id is not None and str(session_id) in bot_ids else CATEGORY_SESSION


def split_unread_counts(
    unread_by_session: Mapping[str, int],
    *,
    bot_session_ids: Iterable[str],
) -> dict[str, int]:
    """Aggregate per-Session unread counts into the two Inbox categories."""

    bot_ids = set(bot_session_ids)
    counts = {CATEGORY_BOT: 0, CATEGORY_SESSION: 0}
    for session_id, count in unread_by_session.items():
        category = CATEGORY_BOT if session_id in bot_ids else CATEGORY_SESSION
        counts[category] += int(count)
    return counts
