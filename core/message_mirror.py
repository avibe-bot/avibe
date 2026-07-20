"""Best-effort mirror of inbound + outbound IM traffic into ``messages``.

This is the cross-platform write path that feeds the workbench: every
non-avibe IM event (Slack DM, Discord channel reply, Telegram private
chat, Lark/Feishu group ping, WeChat push) lands a row in the same
``messages`` table that avibe sessions write to. Downstream views — the
Inbox feed, per-session transcript, future cross-platform search — read
from one shape regardless of origin.

Hooks live in two places:

* ``core/handlers/message_handler.py`` calls :func:`mirror_inbound` once
  per human-originated turn, after session resolution.
* ``core/message_dispatcher.py`` calls :func:`persist_agent_message` once per
  agent ``emit_agent_message`` — for every type, on every platform incl. avibe,
  BEFORE the IM mute filter. Chat outputs land in ``messages``; tool-call trace
  events land in ``agent_events``.

Failures are swallowed and logged. A bad mirror write must never break
the live IM reply path.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from modules.im.base import MessageContext
from storage import agent_events_service, messages_service, settings_service
from storage.db import get_cached_sqlite_engine

logger = logging.getLogger(__name__)

# Non-avibe IM scopes are stored as channel rows. Platforms whose DM chat id is
# literally the user id (Telegram/WeChat style) would otherwise collide with a
# user scope for the same native id, so those DMs are stored as user rows.
# avibe projects are 'project' typed and pre-created via ``/api/projects``; this
# module never touches those.
DEFAULT_SCOPE_TYPE = "channel"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_scope_id(conn, context: MessageContext) -> Optional[str]:
    platform = (context.platform or "").strip()
    is_dm = bool((context.platform_specific or {}).get("is_dm", False))
    use_user_scope = is_dm and context.channel_id and context.user_id and context.channel_id == context.user_id
    scope_type = "user" if use_user_scope else DEFAULT_SCOPE_TYPE
    native_id = ((context.user_id if use_user_scope else context.channel_id) or "").strip()
    if not platform or not native_id:
        return None
    try:
        return settings_service.upsert_scope(
            conn,
            platform=platform,
            scope_type=scope_type,
            native_id=native_id,
            now=_now(),
            supports_threads=bool(context.thread_id),
        )
    except Exception:
        logger.exception("mirror: failed to upsert scope for %s::%s", platform, native_id)
        return None


def _append_quietly(conn, **kwargs) -> Optional[dict]:
    """Insert one row and return its payload, swallowing the unique-constraint
    clash that fires when the same native message id is delivered twice (rare
    retry path). Returns ``None`` on the swallowed duplicate so callers can skip
    the realtime ``message.new`` publish for a row that didn't materialize.
    """
    try:
        return messages_service.append(conn, **kwargs)
    except IntegrityError:
        logger.debug(
            "mirror: skipped duplicate native_message_id %s on platform %s",
            kwargs.get("native_message_id"),
            kwargs.get("platform"),
        )
        return None


def _publish_session_message(row: Optional[dict]) -> None:
    """Publish a session-scoped ``message.new`` for a freshly persisted row.

    The Controller process persists agent + harness rows; this fans the row out
    over ``inbox_events.bus`` → ``/internal/events`` → ``inbox_bridge`` →
    browser ``SSEBroker`` (the #359 path), so an open Chat page appends it live —
    the session/page-scoped stream that replaces per-turn SSE. Scoped to rows
    that carry a ``session_id`` (avibe sessions); IM rows are scope-keyed and the
    workbench Chat is avibe-only, so they have no live consumer.
    """
    if not row or not row.get("session_id"):
        return
    try:
        from core.inbox_events import bus

        bus.publish("message.new", row)
    except Exception:
        logger.debug("message_mirror: message.new publish failed", exc_info=True)


# Streaming intermediate activity (interim ``assistant`` messages + ``tool_call``
# trace) to an open Chat page is gated on ``config.ui.show_agent_activity`` (default
# off). Reading the config parses a file, and this path runs once per agent emit —
# many times per turn — so the flag is cached briefly, keyed by config path so a
# fresh AVIBE_HOME (e.g. per-test) never reads a stale value. On any error the flag
# is OFF: a config glitch degrades to today's strict no-op (no extra stream).
_ACTIVITY_FLAG_TTL_SECONDS = 3.0
_activity_flag_cache: dict[str, tuple[float, bool]] = {}


def reset_activity_flag_cache() -> None:
    """Drop the cached ``show_agent_activity`` flag (test hook / config-change)."""
    _activity_flag_cache.clear()


def _activity_streaming_enabled() -> bool:
    try:
        from config import paths

        key = str(paths.get_config_path())
    except Exception:
        key = ""
    now = time.monotonic()
    cached = _activity_flag_cache.get(key)
    if cached is not None and (now - cached[0]) < _ACTIVITY_FLAG_TTL_SECONDS:
        return cached[1]
    enabled = False
    try:
        from core.services import settings as settings_svc

        enabled = bool(settings_svc.load_config_or_default().ui.show_agent_activity)
    except Exception:
        logger.debug("message_mirror: activity flag read failed", exc_info=True)
        enabled = False
    _activity_flag_cache[key] = (now, enabled)
    return enabled


def _publish_activity_event(event_row: Optional[dict]) -> None:
    """Publish a session-scoped ``message.new`` for a ``tool_call`` trace event.

    Tool-call rows live in ``agent_events`` (never in ``messages`` / the transcript),
    so synthesize a ``messages``-shaped payload the Chat activity group can render
    live. Called only when ``show_agent_activity`` is on; the payload carries its own
    display data (no read-after-announce), so the browser never re-queries the row.
    """
    if not event_row or not event_row.get("session_id"):
        return
    payload = {
        "id": event_row.get("id"),
        "scope_id": event_row.get("scope_id"),
        "session_id": event_row.get("session_id"),
        "platform": event_row.get("platform") or "avibe",
        "author": "agent",
        "type": "tool_call",
        "source": "agent",
        "author_id": None,
        "author_name": event_row.get("agent_name"),
        "native_message_id": None,
        "parent_native_message_id": None,
        "text": event_row.get("text") or "",
        "content": event_row.get("content") or {},
        "metadata": event_row.get("metadata") or {},
        "created_at": event_row.get("created_at"),
        "updated_at": event_row.get("updated_at") or event_row.get("created_at"),
        "delivered_at": None,
        "read_at": None,
    }
    try:
        from core.inbox_events import bus

        bus.publish("message.new", payload)
    except Exception:
        logger.debug("message_mirror: activity tool_call publish failed", exc_info=True)


def _session_row(conn, session_id: str) -> Optional[dict]:
    """Resolve scope/provenance from an agent session."""
    from sqlalchemy import select

    from storage.models import agent_sessions

    row = conn.execute(
        select(
            agent_sessions.c.scope_id,
            agent_sessions.c.agent_name,
            agent_sessions.c.agent_backend,
        ).where(agent_sessions.c.id == session_id)
    ).mappings().first()
    return dict(row) if row else None


def _scope_id_for_session(conn, session_id: str) -> Optional[str]:
    """Resolve a message's scope from its agent session (works for avibe +
    IM once the session has been reserved)."""
    row = _session_row(conn, session_id)
    return row["scope_id"] if row else None


# Maps the dispatcher's canonical message type to the persisted ``messages.type``.
# ``system`` folds into ``assistant`` (a process-log message, not a user-facing
# reply): once terminal-failure ``notify`` rows became inbox-eligible, routine
# system/init logs stored as ``notify`` would have created an Inbox card with a
# junk preview before any real reply. As process log, ``system`` belongs with
# ``assistant`` / ``tool_call`` — out of the inbox and out of the transcript
# (Codex P2). Genuine terminal failures persist via canonical ``notify``.
_AGENT_TYPE_BY_CANONICAL = {
    "result": "result",
    # A terminal FAILED result — kept distinct from ``result`` so unread queries
    # (result-only) don't count it, while it stays in the transcript/inbox.
    "error": "error",
    "notify": "notify",
    "assistant": "assistant",
    "system": "assistant",
}


def _is_tool_call_type(canonical_type: str | None) -> bool:
    return (canonical_type or "") in {"toolcall", "tool_call"}


def _agent_provenance_from_context(context: MessageContext, session_row: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    spec = context.platform_specific or {}
    target = spec.get("agent_session_target") or {}
    agent_name = spec.get("vibe_agent_name") or target.get("agent_name") or (session_row or {}).get("agent_name")
    backend = spec.get("vibe_agent_backend") or target.get("agent_backend") or (session_row or {}).get("agent_backend")
    return agent_name, backend


def _trace_ids_from_context(context: MessageContext) -> tuple[Optional[str], Optional[str]]:
    spec = context.platform_specific or {}
    trigger_kind = str(spec.get("task_trigger_kind") or "").strip()
    run_id = str(spec.get("task_execution_id") or "").strip() or None
    turn_token = str(spec.get("turn_token") or "").strip()
    turn_id = turn_token or None
    if trigger_kind == "agent_run" and run_id and not turn_id:
        turn_id = run_id
    return turn_id, run_id


def persist_silent_completion_marker(context: MessageContext) -> None:
    """Persist the invisible ``silent`` terminal marker for a turn that completed
    NORMALLY but delivered no user-visible message — a ``<silent>``-stripped/empty
    final reply, or a reply-less bookkeeping turn (common for watch/scheduled runs).

    Written ONCE per turn at the delivery chokepoint
    (``MessageDispatcher.emit_agent_message``) on the clean-completion path only — NOT
    for cancel/Stop (which legitimately stays ``interrupted``) nor backend failures
    (which already emit a visible ``notify``). It exists solely so the activity
    grouping closes the turn as DONE instead of misreading "activity + no terminal" as
    interrupted; it is never delivered, or shown as a transcript bubble (see
    ``messages_service.SILENT_TYPE`` and its allowlist/denylist exclusions). Writes via
    ``_append_quietly`` directly — bypassing ``persist_agent_message``'s empty-text
    guard and the ``message.new`` publish (the marker is invisible in the transcript).

    It DOES recompute + publish the inbox row, though: the marker counts as a reply for
    the inbox awaiting/replied flag, so an open sidebar must clear "awaiting the agent"
    live instead of staying stale until a reconnect. No web-push (a silent completion is
    not a notifiable reply). Best-effort: the caller wraps it; a failure must never break
    turn completion.
    """
    if not context.platform:
        return
    session_id = (context.platform_specific or {}).get("agent_session_id")
    engine = get_cached_sqlite_engine()
    inbox_row = None
    with engine.begin() as conn:
        if context.platform == "avibe":
            session_row = _session_row(conn, session_id) if session_id else None
            scope_id = session_row["scope_id"] if session_row else None
        else:
            session_row = None
            scope_id = _resolve_scope_id(conn, context)
        if scope_id is None:
            return
        agent_name, _backend = _agent_provenance_from_context(context, session_row)
        _append_quietly(
            conn,
            scope_id=scope_id,
            session_id=session_id,
            platform=context.platform,
            author="agent",
            source="agent",
            author_name=agent_name,
            message_type=messages_service.SILENT_TYPE,
            text="",
            metadata=None,
            native_message_id=None,
            parent_native_message_id=context.thread_id,
            content={"kind": "silent"},
        )
        # Recompute the session's inbox row so the awaiting/replied flag clears live.
        # avibe-only (the workbench inbox is avibe-scoped; IM rows aren't shown there).
        if context.platform == "avibe" and session_id:
            inbox_row = messages_service.get_inbox_session(conn, session_id)
    if inbox_row is not None:
        # Same ``inbox.session.updated`` event a visible reply publishes — but NOT
        # ``message.new`` (no transcript bubble) and NOT web-push (not a notifiable reply).
        try:
            from core.inbox_events import bus

            bus.publish("inbox.session.updated", inbox_row)
        except Exception:
            logger.debug("persist_silent_completion_marker: inbox publish failed", exc_info=True)


def persist_agent_message(
    context: MessageContext,
    canonical_type: str,
    text: str,
    *,
    quick_replies: Optional[list[str]] = None,
    metadata: Optional[dict[str, Any]] = None,
    native_message_id: Optional[str] = None,
) -> Optional[dict]:
    """Persist one agent output into the workbench ``messages`` store.

    Unified across **all** platforms (including avibe, which has no IM mirror)
    and called BEFORE any IM delivery/mute decision, so assistant messages and
    tool-call trace events land even when a channel hides them. Each ``emit_agent_message``
    call is a distinct logical message — the consolidated IM "log" message only
    merges them for display — so one row per emit is correct, not fragments.

    ``context`` is the **post-routing delivery target** (see
    ``emit_agent_message``): IM rows are attributed to the channel that actually
    received the reply, so routed / ``post_to`` / thread replies are recorded
    under their delivery scope rather than the source session's — keeping
    cross-platform history/search pointed at the right conversation. avibe rows
    instead use the session's project scope (``agent_session_id`` from
    ``context.platform_specific``), which is what the per-session inbox groups on.
    """
    if not text or not text.strip():
        return
    if not context.platform:
        return
    # ``system`` messages are generated by us (init banners, status lines), not the
    # agent — don't persist them at all (they have no place in the transcript and
    # would only be noise in history) (user request).
    if (canonical_type or "") == "system":
        return
    session_id = (context.platform_specific or {}).get("agent_session_id")
    try:
        engine = get_cached_sqlite_engine()
        inbox_row = None
        appended_row = None
        tool_event_row = None
        message_type = None
        is_tool_call = _is_tool_call_type(canonical_type)
        with engine.begin() as conn:
            if context.platform == "avibe":
                # Inbox groups by the avibe session's project scope; never invent
                # a 'channel' scope for avibe (projects are pre-created via
                # /api/projects). Skip if the session row isn't visible yet.
                session_row = _session_row(conn, session_id) if session_id else None
                scope_id = session_row["scope_id"] if session_row else None
                row_session_id = session_id
            else:
                # IM: the row stays SCOPE-keyed to the delivery channel (this
                # ``context`` is the routed target, matching where the reply was
                # sent), but it now ALSO carries the SOURCE session_id (already
                # resolved into ``agent_session_id``) so a session's full transcript
                # is queryable by ``session_id`` on every platform. ``scope_id`` and
                # ``session_id`` can legitimately differ under routing / ``post_to``:
                # the session join answers "what did this session say", the scope
                # grouping answers "what happened in this channel".
                session_row = None
                scope_id = _resolve_scope_id(conn, context)
                row_session_id = session_id
            if scope_id is None:
                return
            # Provenance: every agent reply is source='agent'; name = the
            # session's agent (from the dispatch context). source_id (author_id)
            # is left to the agent-id wiring later; the session already carries it.
            agent_name, backend = _agent_provenance_from_context(context, session_row)
            if is_tool_call:
                turn_id, run_id = _trace_ids_from_context(context)
                # Captured for the (gated) live activity publish after commit; the
                # row itself is trace data in ``agent_events`` (never in ``messages``).
                tool_event_row = agent_events_service.append(
                    conn,
                    scope_id=scope_id,
                    session_id=row_session_id,
                    platform=context.platform,
                    event_type="tool_call",
                    visibility="trace",
                    text=text,
                    content={"kind": canonical_type or "tool_call"},
                    metadata={"canonical_type": canonical_type or "tool_call"},
                    agent_name=agent_name,
                    backend=backend,
                    turn_id=turn_id,
                    run_id=run_id,
                )
            else:
                message_type = _AGENT_TYPE_BY_CANONICAL.get(canonical_type or "", "assistant")
                # Workbench Chat only: rewrite ``file://`` links in the persisted copy
                # to same-origin media-proxy URLs so the browser renders agent images
                # inline + files as download cards. IM rows keep the raw ``file://``
                # (the dispatcher uploads those to the platform separately). Scoped to
                # the user-visible result/notify rows so we don't mint tokens for the
                # hidden intermediate assistant stream.
                if context.platform == "avibe" and message_type in ("result", "notify", "error") and row_session_id:
                    try:
                        from core.workbench_media import rewrite_agent_media

                        text = rewrite_agent_media(
                            conn, scope_id=scope_id, session_id=row_session_id, text=text
                        )
                    except Exception:
                        logger.exception("persist_agent_message: media rewrite failed")
                content: Optional[dict] = {"kind": canonical_type} if canonical_type else None
                # Quick-reply buttons (avibe result): the trailing ``---\n[label]…``
                # block was already parsed + stripped upstream; carry the labels in
                # ``content`` so the workbench renders the button group (IM channels
                # render their own native buttons from the same parse).
                if quick_replies:
                    content = {**(content or {}), "quick_replies": list(quick_replies)}
                appended_row = _append_quietly(
                    conn,
                    scope_id=scope_id,
                    session_id=row_session_id,
                    platform=context.platform,
                    author="agent",
                    source="agent",
                    author_name=agent_name,
                    message_type=message_type,
                    text=text,
                    metadata=metadata,
                    native_message_id=native_message_id,
                    parent_native_message_id=context.thread_id,
                    content=content,
                )
                # Recompute the session's inbox row so the realtime event can patch
                # the browser without a refetch. avibe-only: the workbench inbox is
                # scoped to avibe sessions (IM rows persist but aren't shown there).
                if context.platform == "avibe" and session_id:
                    inbox_row = messages_service.get_inbox_session(conn, session_id)
        # tool_call rows never enter ``messages``/TRANSCRIPT_TYPES; when the Chat
        # activity panel is enabled they fan out here as a synthesized
        # ``message.new`` so an open Chat page shows the step live. Default off →
        # no publish (strict no-op). Return None to preserve the tool_call contract.
        if is_tool_call:
            if context.platform == "avibe" and _activity_streaming_enabled():
                _publish_activity_event(tool_event_row)
            return None
        # Fan the row out to an open Chat page (session-scoped stream), then bump
        # the inbox card. Both ride the controller→browser bridge. Only publish
        # transcript-visible types so the live stream carries EXACTLY what the
        # history fetch returns — EXCEPT interim ``assistant`` rows, which stream
        # only when the Chat activity panel is enabled (they still stay out of the
        # transcript + inbox; the activity group renders them separately).
        # avibe-only: IM rows now carry a session_id too, but the workbench Chat is
        # avibe-only, so keep the live fan-out scoped to avibe (an IM session has no
        # open Chat consumer; publishing it would be dead traffic).
        if context.platform == "avibe" and (
            message_type in messages_service.TRANSCRIPT_TYPES
            or (message_type == "assistant" and _activity_streaming_enabled())
        ):
            _publish_session_message(appended_row)
        if inbox_row is not None:
            from core.inbox_events import bus

            bus.publish("inbox.session.updated", inbox_row)
            try:
                from core.web_push_notifications import maybe_notify_inbox_message

                maybe_notify_inbox_message(appended_row, inbox_row)
            except Exception:
                logger.debug("web push notification scheduling failed", exc_info=True)
        return appended_row
    except Exception:
        logger.exception("persist_agent_message: failure on platform=%s", context.platform)
        return None


def agent_message_exists(context: MessageContext, native_message_id: str | None) -> bool:
    """Check a stable output identity before external delivery.

    The database unique constraint remains the final race guard. This early
    check prevents ordinary callback or backend retries from posting the same
    durable output to an IM surface twice.
    """

    platform = str(context.platform or "").strip()
    identity = str(native_message_id or "").strip()
    if not platform or not identity:
        return False
    try:
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            return messages_service.native_message_exists(
                conn,
                platform=platform,
                native_message_id=identity,
            )
    except Exception:
        logger.debug("agent_message_exists: lookup failed open", exc_info=True)
        return False


def mirror_harness_inbound(context: MessageContext, text: str) -> None:
    """Record a harness-originated prompt (scheduled task / watch / webhook).

    The backend consumes the prompt as turn input, but the persisted row must
    not claim the human authored it. ``author`` and ``type`` therefore use the
    first-class harness role while ``source='harness'`` preserves provenance.
    ``author_name`` carries the trigger kind (scheduled / watch / webhook / ...)
    and ``author_id`` the run-definition id, per the provenance spec.

    Unlike :func:`mirror_inbound` this *does* cover avibe: no REST endpoint
    writes the harness prompt, so without this the workbench transcript would
    show an agent reply with no originating turn. Scope resolution mirrors
    :func:`persist_agent_message` — avibe rows attach to the session's project
    scope, IM rows to the delivery channel.
    """
    if not text or not text.strip():
        return
    if not context.platform:
        return
    spec = context.platform_specific or {}
    trigger_kind = spec.get("task_trigger_kind")
    definition_id = spec.get("task_definition_id")
    session_id = spec.get("agent_session_id")
    try:
        engine = get_cached_sqlite_engine()
        appended_row = None
        inbox_row = None
        with engine.begin() as conn:
            if context.platform == "avibe":
                scope_id = _scope_id_for_session(conn, session_id) if session_id else None
                row_session_id = session_id
            else:
                # Attribute the prompt to the SAME scope the reply lands in. A
                # scheduled/watch run with a delivery override (post_to / a
                # different deliver-key) sends its result to the override channel
                # (see ``emit_agent_message``); resolve the prompt there too so
                # one turn isn't split across the source + delivery scopes (Codex
                # P2). Falls back to the source context when there's no override.
                deliver_ctx = context
                override = spec.get("delivery_override") or {}
                if override.get("channel_id"):
                    deliver_ctx = MessageContext(
                        user_id=override.get("user_id") or context.user_id,
                        channel_id=override["channel_id"],
                        platform=override.get("platform") or context.platform,
                        thread_id=override.get("thread_id"),
                    )
                scope_id = _resolve_scope_id(conn, deliver_ctx)
                # Scope-keyed to the delivery channel, but carry the source
                # session_id too so the harness prompt joins to its session, the
                # same way the agent reply does in ``persist_agent_message``.
                row_session_id = session_id
            if scope_id is None:
                return
            appended_row = _append_quietly(
                conn,
                scope_id=scope_id,
                session_id=row_session_id,
                platform=context.platform,
                author="harness",
                source="harness",
                author_name=trigger_kind,
                author_id=definition_id,
                message_type=messages_service.HARNESS_TYPE,
                text=text,
                native_message_id=context.message_id,
                parent_native_message_id=context.thread_id,
            )
            # Recompute the inbox card so the harness prompt re-ranks the session
            # + flips its activity for other open views (avibe only; the inbox is
            # avibe-scoped). No-op until the session has a result row.
            if context.platform == "avibe" and row_session_id:
                inbox_row = messages_service.get_inbox_session(conn, row_session_id)
        # Surface the harness-triggered prompt on an open Chat page immediately,
        # so the upcoming agent reply isn't shown with no originating turn.
        # avibe-only: IM rows carry a session_id now but have no open Chat consumer.
        if context.platform == "avibe":
            _publish_session_message(appended_row)
        if inbox_row is not None:
            from core.inbox_events import bus

            bus.publish("inbox.session.updated", inbox_row)
    except Exception:
        logger.exception("mirror_harness_inbound: unexpected failure on platform=%s", context.platform)


def mirror_inbound(context: MessageContext, text: str) -> None:
    """Record a human-originated IM message into the messages table.

    Written scope-keyed with ``session_id=None``: the turn's agent session is not
    bound until dispatch, which runs AFTER this inbound mirror. ``message_handler``
    back-fills the session_id via :func:`link_inbound_message_session` once the
    backend binds it, so the human prompt ends up queryable by ``session_id`` like
    the agent reply and every other turn message — only the write order differs.
    """

    if not text or not text.strip():
        return
    if not context.platform:
        return
    if context.platform == "avibe":
        # avibe's REST endpoint already writes through ``messages_service``;
        # mirroring here would double-count the row.
        return
    try:
        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            scope_id = _resolve_scope_id(conn, context)
            if scope_id is None:
                return
            _append_quietly(
                conn,
                scope_id=scope_id,
                # Back-filled post-dispatch by link_inbound_message_session — the
                # session PK isn't known yet at inbound-mirror time.
                session_id=None,
                platform=context.platform,
                author="user",
                source="user",
                message_type="user",
                text=text,
                author_id=context.user_id,
                native_message_id=context.message_id,
                parent_native_message_id=context.thread_id,
            )
    except Exception:
        logger.exception("mirror_inbound: unexpected failure on platform=%s", context.platform)


def link_inbound_message_session(*, platform: str, native_message_id: str, session_id: str) -> None:
    """Back-fill ``session_id`` onto the IM inbound row written by :func:`mirror_inbound`.

    ``message_handler`` calls this once dispatch has bound the turn's session (the PK
    is stamped on ``context.platform_specific['agent_session_id']`` — the same field
    :func:`persist_agent_message` reads for the reply). Keyed by the unique
    ``(platform, native_message_id)``, so it stamps exactly this turn's human prompt
    and only while still unlinked. No-op for avibe (its inbound is written
    session-keyed via REST). Best-effort: a failure must never break the turn.
    """
    if not (platform and native_message_id and session_id) or platform == "avibe":
        return
    try:
        from sqlalchemy import update as _sa_update

        from storage.models import messages as _messages

        engine = get_cached_sqlite_engine()
        with engine.begin() as conn:
            conn.execute(
                _sa_update(_messages)
                .where(
                    _messages.c.platform == platform,
                    _messages.c.native_message_id == str(native_message_id),
                    _messages.c.session_id.is_(None),
                )
                .values(session_id=session_id)
            )
    except Exception:
        logger.debug("link_inbound_message_session: back-fill failed", exc_info=True)
