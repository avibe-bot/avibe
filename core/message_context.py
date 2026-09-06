"""Shared helpers for MessageContext-derived metadata."""

from __future__ import annotations

from typing import Optional

from config.v2_settings import make_thread_settings_key
from modules.im import MessageContext


# Internal-only marker persisted with scheduled Delivery provenance. It keeps
# durable turns from applying the backend-only metadata a second time when the
# stored dispatch text is finally handed to MessageHandler.


def resolve_context_platform(
    context: Optional[MessageContext],
    *,
    fallback_platform: Optional[str] = None,
    default: str = "",
) -> str:
    """Resolve a MessageContext platform using the common precedence order."""
    platform = fallback_platform or default
    if context is not None:
        payload = context.platform_specific or {}
        platform = context.platform or payload.get("platform") or platform
    return str(platform or default)


def resolve_context_settings_key(context: MessageContext) -> str:
    payload = context.platform_specific or {}
    value = context.user_id if payload.get("is_dm", False) else context.channel_id
    return str(value or "")


def resolve_context_thread_id(context: MessageContext) -> Optional[str]:
    """Return the canonical configurable thread ID for a message context."""
    platform = resolve_context_platform(context)
    payload = context.platform_specific or {}
    if platform != "telegram" or payload.get("is_dm", False):
        return None
    if context.thread_id:
        return str(context.thread_id)
    if payload.get("is_forum") or payload.get("is_topic_message"):
        return "1"
    return None


def build_thread_session_anchor(platform: str, channel_id: str, thread_id: str) -> str:
    """Build a backend session anchor unique at the platform's thread scope."""
    if platform == "telegram":
        return f"{platform}_{channel_id}_{thread_id}"
    return f"{platform}_{thread_id}"


def build_thread_session_anchor_candidates(
    platform: str,
    channel_id: str,
    thread_id: str,
) -> tuple[str, ...]:
    """Return the canonical anchor followed by any compatible legacy anchor."""
    canonical = build_thread_session_anchor(platform, channel_id, thread_id)
    legacy = f"{platform}_{thread_id}"
    return (canonical, legacy) if legacy != canonical else (canonical,)


def thread_id_from_session_anchor(
    anchor: str,
    *,
    platform: str,
    channel_id: str,
) -> Optional[str]:
    """Recover a thread ID from canonical and pre-scoped session anchors."""
    base_anchor = str(anchor or "").split(":", 1)[0]
    if not base_anchor:
        return None

    if platform == "telegram":
        canonical_prefix = f"{platform}_{channel_id}_"
        if base_anchor.startswith(canonical_prefix):
            thread_id = base_anchor[len(canonical_prefix) :]
            return thread_id or None

    legacy_prefix = f"{platform}_"
    if not base_anchor.startswith(legacy_prefix):
        return None
    thread_id = base_anchor[len(legacy_prefix) :]
    return thread_id if thread_id and thread_id != str(channel_id) else None


def resolve_context_scope_settings_key(context: MessageContext) -> str:
    """Resolve the context-aware settings key without changing session identity."""
    base = resolve_context_settings_key(context)
    thread_id = resolve_context_thread_id(context)
    if thread_id and not (context.platform_specific or {}).get("is_dm", False):
        return make_thread_settings_key(base, thread_id)
    return base


def requires_typed_user_session_key(context: MessageContext) -> bool:
    payload = context.platform_specific or {}
    return bool(payload.get("is_dm", False) and context.user_id and context.channel_id == context.user_id)


def build_context_session_key(
    context: MessageContext,
    *,
    platform: Optional[str] = None,
    settings_key: Optional[str] = None,
    fallback_platform: Optional[str] = None,
) -> str:
    resolved_platform = platform or resolve_context_platform(context, fallback_platform=fallback_platform)
    resolved_settings_key = settings_key if settings_key is not None else resolve_context_settings_key(context)
    if requires_typed_user_session_key(context):
        return f"{resolved_platform}::user::{resolved_settings_key}"
    return f"{resolved_platform}::{resolved_settings_key}"


def build_context_turn_sink_key(
    context: MessageContext,
    *,
    session_key: Optional[str] = None,
) -> str:
    """Build the key that identifies ONE agent session's live turn sink.

    Distinct from ``build_context_session_key`` on purpose. That key is
    channel-scoped (``resolve_context_settings_key`` deliberately drops the
    thread), which is right for polls, settings lookup, and message
    consolidation — they are channel-wide concerns. The turn sink is not: it
    is the concurrency slot for a SINGLE agent session, and
    ``dispatch_turn_with_outcome`` refuses any turn that finds the slot taken.

    Keying the sink at channel scope therefore made the refusal channel-wide:
    a long-running Telegram forum topic held the only slot for its whole group,
    so every OTHER topic's turn was refused with ``refused_concurrent_turn``
    before reaching a backend, silently, until that unrelated topic finished.

    The thread scope is resolved the same way every session-anchor caller
    resolves it (``resolve_context_thread_id(context) or context.thread_id``),
    so the key is derived purely from context fields that are already pinned
    before dispatch and carried by every context in a turn's lifetime — the
    dispatch context, the backend receiver's stale per-turn context, and an
    external stop's rebuilt context all land on the same key.
    """

    base = session_key if session_key is not None else build_context_session_key(context)
    # ``getattr`` because this is reached from every turn-lifecycle context, including
    # the lookalike namespaces the workbench/stop paths build. A context without the
    # attribute has no thread, which is the unthreaded case — not an error.
    thread_id = resolve_context_thread_id(context) or getattr(context, "thread_id", None)
    if not thread_id:
        return base
    return f"{base}::thread::{thread_id}"


def resolve_turn_sink_key(controller: object, context: MessageContext) -> str:
    """Resolve ``context``'s turn-sink key, preferring ``controller``'s own accessor.

    Falls back to ``_get_session_key`` and then to the context alone, applying the
    same thread-scoping rule at every step. Never degrades to the bare channel key:
    that scope is what made one busy thread refuse its siblings' turns, so a
    controller without the accessor must still land on a thread-scoped key rather
    than silently reintroduce the collision.

    Always returns a key. A key with no sink registered simply misses in
    ``get_turn_sink``, which is what every caller already handles.
    """

    getter = getattr(controller, "_get_turn_sink_key", None)
    if callable(getter):
        return getter(context)
    base_getter = getattr(controller, "_get_session_key", None)
    if callable(base_getter):
        return build_context_turn_sink_key(context, session_key=base_getter(context))
    return build_context_turn_sink_key(context)
