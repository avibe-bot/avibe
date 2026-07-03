from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, and_, select

from config import paths
from storage.db import create_sqlite_engine
from storage.migrations import guard_source_checkout_default_state_migration, run_migrations
from storage.models import agent_events, media_objects, messages, scope_settings, scopes, state_meta
from storage.settings_service import make_scope_id, upsert_scope

logger = logging.getLogger(__name__)

CHANNEL_SCOPE_TYPE = "channel"
GUILD_SCOPE_TYPE = "guild"
VISIBILITY_VISIBLE = "visible"
VISIBILITY_NOT_RETURNED = "not_returned"
VISIBILITY_UNKNOWN = "unknown"

METADATA_USERNAME = "username"
METADATA_TOPIC = "topic"
METADATA_PLATFORM_ARCHIVED = "platform_archived"
METADATA_VISIBILITY_STATUS = "visibility_status"
METADATA_IS_MEMBER = "is_member"
METADATA_LAST_REFRESHED_AT = "last_refreshed_at"
METADATA_LAST_MISSING_AT = "last_missing_at"
METADATA_IS_FORUM = "is_forum"
METADATA_SUPPORTS_TOPICS = "supports_topics"
METADATA_CHANNEL_POSITION = "channel_position"
METADATA_CHANNEL_CATEGORY_ID = "channel_category_id"
METADATA_CHAT_MODE = "chat_mode"
METADATA_AUTH_CONTEXT = "auth_context"
# Set when a user removes a stale channel that still owns history (messages /
# events / media). The scope row is kept so the CASCADE FKs do not wipe history;
# this flag hides it from every discovery listing. Cleared on rediscovery.
METADATA_DISMISSED_AT = "dismissed_at"

_STICKY_TRUE_KEYS = {METADATA_IS_FORUM, METADATA_SUPPORTS_TOPICS}
_DEBOUNCE_SECONDS = 60.0
_REFRESH_TTL_SECONDS = 300.0
_MIN_REFRESH_INTERVAL_SECONDS = 30.0

_debounce_lock = threading.Lock()
_debounce_cache: dict[tuple[str, str], tuple[float, tuple[Any, ...]]] = {}

_refresh_locks_lock = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}
_scheduled_refreshes_lock = threading.Lock()
_scheduled_refreshes: set[str] = set()
_migration_lock = threading.Lock()
_legacy_migration_lock = threading.Lock()
_migrated_db_paths: set[Path] = set()


@dataclass
class ChannelInfo:
    platform: str
    chat_id: str
    name: str = ""
    native_type: str = ""
    is_private: bool = False
    supports_threads: bool = False
    platform_archived: bool = False
    visibility_status: str = VISIBILITY_UNKNOWN
    is_member: bool | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    configured: bool = False
    first_seen_at: str = ""
    last_seen_at: str = ""

    def to_channel_payload(self) -> dict[str, Any]:
        payload = {
            "id": self.chat_id,
            "name": self.name or self.chat_id,
            "type": _payload_native_type(self.native_type),
            "native_type": self.native_type,
            "is_private": self.is_private,
            "supports_threads": self.supports_threads,
            "platform_archived": self.platform_archived,
            "visibility_status": self.visibility_status,
            "is_member": self.is_member,
            "parent_id": self.parent_id,
            "metadata": self.metadata,
            "configured": self.configured,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
        }
        if METADATA_USERNAME in self.metadata:
            payload["username"] = self.metadata.get(METADATA_USERNAME)
        if METADATA_IS_FORUM in self.metadata:
            payload["is_forum"] = bool(self.metadata.get(METADATA_IS_FORUM))
        if METADATA_SUPPORTS_TOPICS in self.metadata:
            payload["supports_topics"] = bool(self.metadata.get(METADATA_SUPPORTS_TOPICS))
        return payload


@dataclass
class RefreshState:
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None


@dataclass
class RefreshResult:
    ok: bool
    refresh_state: RefreshState
    error: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _json_load_any(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _db_path(db_path: Path | None = None) -> Path:
    return (db_path or paths.get_sqlite_state_path()).expanduser().resolve()


def _ensure_sqlite(db_path: Path | None = None) -> Path:
    target = _db_path(db_path)
    guard_source_checkout_default_state_migration(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _migration_lock:
        if target not in _migrated_db_paths:
            run_migrations(target)
            _migrated_db_paths.add(target)
    return target


def _engine(db_path: Path | None = None):
    return create_sqlite_engine(_ensure_sqlite(db_path))


def _refresh_state_key(platform: str, refresh_scope: str | None = None) -> str:
    key = f"channel_refresh.{platform}"
    return f"{key}.{refresh_scope}" if refresh_scope else key


def _refresh_scope_for(platform: str, kwargs: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if platform == "discord":
        guild_id = str(kwargs.get("guild_id") or "").strip()
        if guild_id:
            parts.append(f"guild.{guild_id}")
    auth_context = _auth_context_for(platform, kwargs)
    if auth_context:
        parts.append(auth_context)
    if platform == "slack" and bool(kwargs.get("browse_all", False)):
        parts.append("browse_all")
    return ".".join(parts) or None


def _auth_context_for(platform: str, kwargs: dict[str, Any]) -> str | None:
    if platform in {"slack", "discord"}:
        bot_token = str(kwargs.get("bot_token") or "").strip()
        if bot_token:
            return f"auth.{_stable_secret_hash(bot_token)}"
    if platform == "lark":
        app_id = str(kwargs.get("app_id") or "").strip()
        app_secret = str(kwargs.get("app_secret") or "").strip()
        domain = str(kwargs.get("domain") or "feishu").strip()
        if app_id and app_secret:
            return f"auth.{_stable_secret_hash(chr(0).join([domain, app_id, app_secret]))}"
    return None


def _stable_secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def get_state_meta(key: str, *, db_path: Path | None = None) -> Any:
    engine = _engine(db_path)
    try:
        with engine.connect() as conn:
            value = conn.execute(select(state_meta.c.value_json).where(state_meta.c.key == key)).scalar_one_or_none()
            return _json_load_any(value)
    finally:
        engine.dispose()


def set_state_meta(key: str, value: Any, *, db_path: Path | None = None, now: str | None = None) -> None:
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            _set_state_meta(conn, key, value, now=now or _utc_now_iso())
    finally:
        engine.dispose()


def _set_state_meta(conn: Connection, key: str, value: Any, *, now: str) -> None:
    conn.execute(state_meta.delete().where(state_meta.c.key == key))
    conn.execute(state_meta.insert().values(key=key, value_json=_json_dumps(value), updated_at=now))


def refresh_state(platform: str, *, refresh_scope: str | None = None, db_path: Path | None = None) -> RefreshState:
    payload = get_state_meta(_refresh_state_key(platform, refresh_scope), db_path=db_path)
    if not isinstance(payload, dict):
        return RefreshState()
    return RefreshState(
        last_attempt_at=payload.get("last_attempt_at"),
        last_success_at=payload.get("last_success_at"),
        last_error=payload.get("last_error"),
    )


def _write_refresh_state(
    conn: Connection,
    platform: str,
    state: RefreshState,
    *,
    now: str,
    refresh_scope: str | None = None,
) -> None:
    _set_state_meta(
        conn,
        _refresh_state_key(platform, refresh_scope),
        asdict(state),
        now=now,
    )


def normalize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {str(key): value for key, value in metadata.items()}


def merge_metadata(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(existing or {})
    for key, value in normalize_metadata(incoming).items():
        if key in _STICKY_TRUE_KEYS and merged.get(key) is True and value is False:
            continue
        merged[key] = value
    return merged


def remember_chat(
    platform: str,
    chat_id: str,
    *,
    name: str = "",
    native_type: str = "",
    is_private: bool | None = None,
    supports_threads: bool | None = None,
    parent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    platform = str(platform)
    chat_id = str(chat_id).strip()
    if not platform or not chat_id:
        return

    normalized_metadata = normalize_metadata(metadata)
    normalized_metadata.setdefault(METADATA_VISIBILITY_STATUS, VISIBILITY_VISIBLE)
    # Seeing the chat again (e.g. Telegram passive rediscovery) un-dismisses it,
    # so a removed-but-kept group reappears once it is active again. Without this,
    # list_chats would hide it forever after a dismissal.
    normalized_metadata.setdefault(METADATA_DISMISSED_AT, None)
    debounce_payload = (
        name,
        native_type,
        is_private,
        supports_threads,
        parent_id,
        tuple(sorted(normalized_metadata.items())),
    )
    debounce_key = (platform, chat_id)
    monotonic_now = time.monotonic()
    with _debounce_lock:
        cached = _debounce_cache.get(debounce_key)
        if cached is not None and monotonic_now - cached[0] < _DEBOUNCE_SECONDS and cached[1] == debounce_payload:
            return

    now = _utc_now_iso()
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            row = _scope_row(conn, platform, CHANNEL_SCOPE_TYPE, chat_id)
            existing_metadata = _json_loads(row["metadata_json"], {}) if row else {}
            merged_metadata = merge_metadata(existing_metadata, normalized_metadata)
            if row is not None:
                row_is_private = bool(row["is_private"])
                row_supports_threads = bool(row["supports_threads"])
                final_is_private = row_is_private or bool(is_private) if is_private is not None else row_is_private
                final_supports_threads = (
                    row_supports_threads or bool(supports_threads)
                    if supports_threads is not None
                    else row_supports_threads
                )
                unchanged = (
                    (not name or row["display_name"] == name)
                    and (not native_type or row["native_type"] == native_type)
                    and row_is_private == final_is_private
                    and row_supports_threads == final_supports_threads
                    and existing_metadata == merged_metadata
                    and _seconds_since(row["last_seen_at"]) < _DEBOUNCE_SECONDS
                )
                if unchanged:
                    with _debounce_lock:
                        _debounce_cache[debounce_key] = (monotonic_now, debounce_payload)
                    return
                is_private = final_is_private
                supports_threads = final_supports_threads

            upsert_scope(
                conn,
                platform,
                CHANNEL_SCOPE_TYPE,
                chat_id,
                parent_scope_id=parent_id,
                display_name=name,
                native_type=native_type,
                is_private=is_private,
                supports_threads=supports_threads,
                metadata=merged_metadata,
                now=now,
            )
    finally:
        engine.dispose()
    with _debounce_lock:
        _debounce_cache[debounce_key] = (monotonic_now, debounce_payload)


def list_chats(
    platform: str,
    *,
    include_private: bool = True,
    include_not_returned: bool = True,
    parent_scope_id: str | None = None,
    auth_context: str | None = None,
    db_path: Path | None = None,
) -> list[ChannelInfo]:
    engine = _engine(db_path)
    try:
        with engine.connect() as conn:
            conditions = [scopes.c.platform == platform, scopes.c.scope_type == CHANNEL_SCOPE_TYPE]
            if not include_private:
                conditions.append(scopes.c.is_private == 0)
            if parent_scope_id is not None:
                conditions.append(scopes.c.parent_scope_id == parent_scope_id)
            query = (
                select(
                    scopes.c.platform,
                    scopes.c.native_id,
                    scopes.c.parent_scope_id,
                    scopes.c.display_name,
                    scopes.c.native_type,
                    scopes.c.is_private,
                    scopes.c.supports_threads,
                    scopes.c.metadata_json,
                    scopes.c.first_seen_at,
                    scopes.c.last_seen_at,
                    scope_settings.c.scope_id.label("settings_scope_id"),
                )
                .select_from(scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id))
                .where(and_(*conditions))
            )
            result: list[ChannelInfo] = []
            for row in conn.execute(query).mappings():
                metadata = _json_loads(row["metadata_json"], {})
                if auth_context is not None and metadata.get(METADATA_AUTH_CONTEXT) != auth_context:
                    continue
                if metadata.get(METADATA_DISMISSED_AT):
                    # User removed this stale entry but history kept the row alive;
                    # never surface it in any discovery listing.
                    continue
                visibility = str(metadata.get(METADATA_VISIBILITY_STATUS) or VISIBILITY_UNKNOWN)
                if visibility == VISIBILITY_NOT_RETURNED and not include_not_returned:
                    continue
                result.append(
                    ChannelInfo(
                        platform=str(row["platform"]),
                        chat_id=str(row["native_id"]),
                        name=str(row["display_name"] or ""),
                        native_type=str(row["native_type"] or ""),
                        is_private=bool(row["is_private"]),
                        supports_threads=bool(row["supports_threads"]),
                        platform_archived=bool(metadata.get(METADATA_PLATFORM_ARCHIVED, False)),
                        visibility_status=visibility,
                        is_member=metadata.get(METADATA_IS_MEMBER),
                        parent_id=row["parent_scope_id"],
                        metadata=metadata,
                        configured=row["settings_scope_id"] is not None,
                        first_seen_at=str(row["first_seen_at"] or ""),
                        last_seen_at=str(row["last_seen_at"] or ""),
                    )
                )
            return sorted(result, key=_channel_sort_key)
    finally:
        engine.dispose()


def list_channel_payloads(platform: str, **kwargs: Any) -> list[dict[str, Any]]:
    return [chat.to_channel_payload() for chat in list_chats(platform, **kwargs)]


def refresh_platform(
    platform: str,
    *,
    force: bool = False,
    db_path: Path | None = None,
    **kwargs: Any,
) -> RefreshResult:
    platform = str(platform)
    refresh_scope = _refresh_scope_for(platform, kwargs)
    auth_context = _auth_context_for(platform, kwargs)
    lock = _refresh_lock(platform, refresh_scope)
    with lock:
        existing_state = refresh_state(platform, refresh_scope=refresh_scope, db_path=db_path)
        if not force and _seconds_since(existing_state.last_attempt_at) < _MIN_REFRESH_INTERVAL_SECONDS:
            return RefreshResult(ok=True, refresh_state=existing_state)

        now = _utc_now_iso()
        engine = _engine(db_path)
        try:
            with engine.begin() as conn:
                _write_refresh_state(
                    conn,
                    platform,
                    RefreshState(
                        last_attempt_at=now,
                        last_success_at=existing_state.last_success_at,
                        last_error=None,
                    ),
                    now=now,
                    refresh_scope=refresh_scope,
                )
        finally:
            engine.dispose()

        try:
            rows, refreshed_parent, fetch_complete = _fetch_platform_channels(
                platform, db_path=db_path, **kwargs
            )
        except Exception as exc:
            error = str(exc)
            logger.warning("Failed to refresh %s channel inventory: %s", platform, error, exc_info=True)
            failed = RefreshState(
                last_attempt_at=now,
                last_success_at=existing_state.last_success_at,
                last_error=error,
            )
            _store_refresh_state(platform, failed, refresh_scope=refresh_scope, db_path=db_path)
            return RefreshResult(ok=False, refresh_state=failed, error=error)

        success_at = _utc_now_iso()
        engine = _engine(db_path)
        try:
            with engine.begin() as conn:
                seen_ids: set[str] = set()
                for row in rows:
                    native_id = str(row.get("id") or "").strip()
                    if not native_id:
                        continue
                    seen_ids.add(native_id)
                    parent_scope_id = row.get("parent_scope_id")
                    metadata = merge_metadata(
                        row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                        {
                            METADATA_VISIBILITY_STATUS: VISIBILITY_VISIBLE,
                            METADATA_LAST_REFRESHED_AT: success_at,
                            METADATA_LAST_MISSING_AT: None,
                            # A channel that reappears on the platform is no longer
                            # dismissed — surface it again.
                            METADATA_DISMISSED_AT: None,
                            METADATA_AUTH_CONTEXT: auth_context,
                        },
                    )
                    upsert_scope(
                        conn,
                        platform,
                        CHANNEL_SCOPE_TYPE,
                        native_id,
                        parent_scope_id=parent_scope_id,
                        display_name=str(row.get("name") or ""),
                        native_type=str(row.get("native_type") or row.get("type") or ""),
                        is_private=bool(row.get("is_private", False)),
                        supports_threads=bool(row.get("supports_threads", False)),
                        metadata=metadata,
                        now=success_at,
                    )
                if fetch_complete:
                    _mark_not_returned(
                        conn,
                        platform,
                        seen_ids,
                        refreshed_at=success_at,
                        parent_scope_id=refreshed_parent,
                        auth_context=auth_context,
                        require_member=platform == "slack" and not bool(kwargs.get("browse_all", False)),
                    )
                else:
                    logger.warning(
                        "Skipping not_returned marking for %s: live inventory was incomplete/truncated",
                        platform,
                    )
                state = RefreshState(last_attempt_at=now, last_success_at=success_at, last_error=None)
                _write_refresh_state(conn, platform, state, now=success_at, refresh_scope=refresh_scope)
                if platform == "slack" and bool(kwargs.get("browse_all", False)) and auth_context:
                    _write_refresh_state(conn, platform, state, now=success_at, refresh_scope=auth_context)
                return RefreshResult(ok=True, refresh_state=state)
        finally:
            engine.dispose()


def should_refresh(
    platform: str,
    *,
    refresh_scope: str | None = None,
    db_path: Path | None = None,
    ttl_seconds: float = _REFRESH_TTL_SECONDS,
) -> bool:
    state = refresh_state(platform, refresh_scope=refresh_scope, db_path=db_path)
    return (
        _seconds_since(state.last_success_at) >= ttl_seconds
        and _seconds_since(state.last_attempt_at) >= _MIN_REFRESH_INTERVAL_SECONDS
    )


def migrate_legacy_discovered_chats(*, db_path: Path | None = None, legacy_path: Path | None = None) -> None:
    with _legacy_migration_lock:
        _migrate_legacy_discovered_chats_unlocked(db_path=db_path, legacy_path=legacy_path)


def _migrate_legacy_discovered_chats_unlocked(*, db_path: Path | None = None, legacy_path: Path | None = None) -> None:
    marker = "migrations.discovered_chats_to_scopes"
    if get_state_meta(marker, db_path=db_path) == "done":
        return

    source = legacy_path or paths.get_discovered_chats_path()
    if not source.exists():
        set_state_meta(marker, "done", db_path=db_path)
        return

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        set_state_meta(marker, "done", db_path=db_path)
        return
    except Exception as exc:
        logger.warning("Skipping malformed legacy discovered chats file %s: %s", source, exc)
        return
    platforms = payload.get("platforms") if isinstance(payload, dict) else {}
    if not isinstance(platforms, dict):
        logger.warning("Skipping legacy discovered chats file %s: platforms must be an object", source)
        return

    for platform, chats in platforms.items():
        if not isinstance(chats, dict):
            continue
        for chat_id, chat in chats.items():
            if not isinstance(chat, dict):
                continue
            remember_chat(
                str(platform),
                str(chat_id),
                name=str(chat.get("name") or ""),
                native_type=str(chat.get("chat_type") or ""),
                is_private=bool(chat.get("is_private", False)),
                supports_threads=bool(chat.get("supports_topics", False)),
                metadata={
                    METADATA_USERNAME: chat.get("username"),
                    METADATA_IS_FORUM: bool(chat.get("is_forum", False)),
                    METADATA_SUPPORTS_TOPICS: bool(chat.get("supports_topics", False)),
                },
                db_path=db_path,
            )

    migrated_path = source.with_suffix(f"{source.suffix}.migrated")
    _rename_preserving_existing(source, migrated_path)
    set_state_meta(marker, "done", db_path=db_path)


def channels_response(
    platform: str,
    *,
    force: bool = False,
    include_private: bool = True,
    require_member: bool = False,
    include_not_returned: bool = False,
    parent_scope_id: str | None = None,
    db_path: Path | None = None,
    **refresh_kwargs: Any,
) -> dict[str, Any]:
    migrate_legacy_discovered_chats(db_path=db_path)
    refresh_scope = _refresh_scope_for(platform, refresh_kwargs)
    auth_context = _auth_context_for(platform, refresh_kwargs)
    can_refresh = platform in {"slack", "discord", "lark"}
    if can_refresh and auth_context is None:
        return {
            "ok": False,
            "channels": [],
            "chats": [],
            "refreshing": False,
            "last_attempt_at": None,
            "last_success_at": None,
            "error": f"Missing {platform} channel refresh credentials",
            "summary": _summary([], [], include_private=include_private),
        }
    all_chats = list_chats(
        platform,
        include_private=True,
        parent_scope_id=parent_scope_id,
        auth_context=auth_context,
        db_path=db_path,
    )
    chats = _filter_response_chats(
        all_chats,
        include_private=include_private,
        require_member=require_member,
        include_not_returned=include_not_returned,
    )
    state = refresh_state(platform, refresh_scope=refresh_scope, db_path=db_path)
    refreshing = False
    error = state.last_error

    cache_requires_refresh = _response_cache_requires_refresh(platform, state, refresh_kwargs)
    # Trigger an initial refresh only when we have never successfully fetched.
    # Do NOT key off an empty filtered ``chats`` list: once stale channels are
    # hidden, an all-deleted install would otherwise refresh on every request and
    # bypass the per-platform TTL guard (which lives inside ``refresh_platform``).
    if can_refresh and (force or state.last_success_at is None or cache_requires_refresh):
        result = refresh_platform(platform, force=force, db_path=db_path, **refresh_kwargs)
        state = result.refresh_state
        error = result.error or state.last_error
        all_chats = list_chats(
            platform,
            include_private=True,
            parent_scope_id=parent_scope_id,
            auth_context=auth_context,
            db_path=db_path,
        )
        chats = _filter_response_chats(
            all_chats,
            include_private=include_private,
            require_member=require_member,
            include_not_returned=include_not_returned,
        )
    elif can_refresh and should_refresh(platform, refresh_scope=refresh_scope, db_path=db_path):
        refreshing = _schedule_refresh(platform, db_path=db_path, **refresh_kwargs)

    channels = [chat.to_channel_payload() for chat in chats]
    return {
        "ok": bool(channels) or error is None,
        "channels": channels,
        "chats": channels,
        "refreshing": refreshing,
        "last_attempt_at": state.last_attempt_at,
        "last_success_at": state.last_success_at,
        "error": error,
        "summary": _summary(all_chats, chats, include_private=include_private),
    }


def _fetch_platform_channels(
    platform: str, **kwargs: Any
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Fetch the live channel inventory for a platform.

    Returns ``(rows, parent_scope_id, fetch_complete)``. ``fetch_complete`` is
    ``False`` when the live list was truncated/partial (e.g. Lark page cap), in
    which case the caller must NOT mark unseen channels ``not_returned``.
    """
    from vibe import api

    if platform == "slack":
        result = api.list_channels_live(
            kwargs.get("bot_token", ""),
            browse_all=bool(kwargs.get("browse_all", False)),
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Slack channel refresh failed"))
        rows = []
        for channel in result.get("channels") or []:
            is_member = True if result.get("is_member_only") else channel.get("is_member")
            rows.append(
                {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "native_type": channel.get("type") or ("private_channel" if channel.get("is_private") else "public_channel"),
                    "is_private": bool(channel.get("is_private", False)),
                    "metadata": {
                        METADATA_USERNAME: channel.get("name"),
                        METADATA_IS_MEMBER: is_member,
                    },
                }
            )
        return rows, None, True

    if platform == "discord":
        guild_id = str(kwargs.get("guild_id") or "").strip()
        if not guild_id:
            raise RuntimeError("Discord guild_id is required")
        parent_scope_id = make_scope_id("discord", GUILD_SCOPE_TYPE, guild_id)
        _remember_guild("discord", guild_id, db_path=kwargs.get("db_path"))
        result = api.discord_list_channels_live(kwargs.get("bot_token", ""), guild_id)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Discord channel refresh failed"))
        rows = []
        for channel in result.get("channels") or []:
            channel_type = channel.get("type")
            rows.append(
                {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "native_type": "" if channel_type is None else str(channel_type),
                    "is_private": False,
                    "supports_threads": channel_type in (0, 5),
                    "parent_scope_id": parent_scope_id,
                    "metadata": {
                        METADATA_CHANNEL_POSITION: channel.get("position"),
                        METADATA_CHANNEL_CATEGORY_ID: channel.get("parent_id"),
                    },
                }
            )
        return rows, parent_scope_id, True

    if platform == "lark":
        result = api.lark_list_chats_live(
            kwargs.get("app_id", ""),
            kwargs.get("app_secret", ""),
            kwargs.get("domain", "feishu"),
        )
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Lark chat refresh failed"))
        rows = []
        for chat in result.get("channels") or []:
            rows.append(
                {
                    "id": chat.get("id"),
                    "name": chat.get("name"),
                    "native_type": str(chat.get("chat_type") or ""),
                    "is_private": bool(chat.get("is_private", False)),
                    "metadata": {METADATA_CHAT_MODE: chat.get("chat_mode") or chat.get("chat_type")},
                }
            )
        # A truncated page-cap result is an incomplete inventory: never use it to
        # mark unseen chats not_returned.
        fetch_complete = not bool(result.get("truncated", False))
        return rows, None, fetch_complete

    raise RuntimeError(f"Unsupported active refresh platform: {platform}")


def _remember_guild(platform: str, guild_id: str, *, db_path: Path | None = None) -> None:
    now = _utc_now_iso()
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            upsert_scope(conn, platform, GUILD_SCOPE_TYPE, guild_id, now=now)
    finally:
        engine.dispose()


def _mark_not_returned(
    conn: Connection,
    platform: str,
    seen_ids: set[str],
    *,
    refreshed_at: str,
    parent_scope_id: str | None,
    auth_context: str | None,
    require_member: bool = False,
) -> None:
    conditions = [scopes.c.platform == platform, scopes.c.scope_type == CHANNEL_SCOPE_TYPE]
    if parent_scope_id is not None:
        conditions.append(scopes.c.parent_scope_id == parent_scope_id)
    rows = conn.execute(select(scopes).where(and_(*conditions))).mappings()
    for row in rows:
        native_id = str(row["native_id"])
        if native_id in seen_ids:
            continue
        metadata = _json_loads(row["metadata_json"], {})
        if auth_context is not None and metadata.get(METADATA_AUTH_CONTEXT) != auth_context:
            continue
        if require_member and metadata.get(METADATA_IS_MEMBER) is not True:
            continue
        metadata[METADATA_VISIBILITY_STATUS] = VISIBILITY_NOT_RETURNED
        metadata[METADATA_LAST_MISSING_AT] = refreshed_at
        conn.execute(
            scopes.update()
            .where(scopes.c.id == row["id"])
            .values(metadata_json=_json_dumps(metadata), updated_at=refreshed_at)
        )


def _scope_has_history(conn: Connection, scope_id: str) -> bool:
    """True if the scope owns rows whose FK to scopes is ON DELETE CASCADE.

    ``messages``, ``agent_events`` and ``media_objects`` cascade-delete with the
    scope row, so deleting a scope that owns any of them would destroy stored
    chat history/traces/media — not just the discovery entry.
    """
    for table in (messages, agent_events, media_objects):
        exists = conn.execute(
            select(table.c.scope_id).where(table.c.scope_id == scope_id).limit(1)
        ).first()
        if exists is not None:
            return True
    return False


def delete_scope(
    platform: str,
    native_id: str,
    *,
    scope_type: str = CHANNEL_SCOPE_TYPE,
    db_path: Path | None = None,
) -> dict[str, bool]:
    """Remove a discovered scope and its settings without destroying history.

    The user-facing intent is "clear this stale entry", not "purge all stored
    messages". Because ``messages`` / ``agent_events`` / ``media_objects`` FK the
    scope with ON DELETE CASCADE, a hard delete would wipe that history. So:

    - the ``scope_settings`` row is always deleted (the user's config);
    - if the scope owns no cascading history, the ``scopes`` row is physically
      deleted (clean removal);
    - otherwise the ``scopes`` row is kept and stamped ``dismissed_at`` so it is
      hidden from every discovery listing while its history stays intact.

    Returns ``{"removed": bool, "dismissed": bool}``.
    """
    scope_id = make_scope_id(platform, scope_type, native_id)
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            row = conn.execute(select(scopes).where(scopes.c.id == scope_id)).mappings().one_or_none()
            if row is None:
                return {"removed": False, "dismissed": False}
            conn.execute(scope_settings.delete().where(scope_settings.c.scope_id == scope_id))
            if _scope_has_history(conn, scope_id):
                metadata = _json_loads(row["metadata_json"], {})
                metadata[METADATA_DISMISSED_AT] = _utc_now_iso()
                conn.execute(
                    scopes.update()
                    .where(scopes.c.id == scope_id)
                    .values(metadata_json=_json_dumps(metadata), updated_at=_utc_now_iso())
                )
                return {"removed": False, "dismissed": True}
            # No cascading history — safe to physically delete. Child scopes keep
            # their rows (parent_scope_id is ON DELETE SET NULL).
            result = conn.execute(scopes.delete().where(scopes.c.id == scope_id))
            return {"removed": bool(result.rowcount), "dismissed": False}
    finally:
        engine.dispose()


def _scope_row(conn: Connection, platform: str, scope_type: str, native_id: str):
    scope_id = make_scope_id(platform, scope_type, native_id)
    return conn.execute(select(scopes).where(scopes.c.id == scope_id)).mappings().one_or_none()


def _seconds_since(value: str | None) -> float:
    if not value:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _channel_sort_key(chat: ChannelInfo) -> tuple[int, int, str, str]:
    visibility_rank = 0 if chat.visibility_status == VISIBILITY_VISIBLE else 1
    configured_rank = 0 if chat.configured else 1
    return (visibility_rank, configured_rank, (chat.name or chat.chat_id).lower(), chat.chat_id)


def _refresh_lock(platform: str, refresh_scope: str | None = None) -> threading.Lock:
    key = _refresh_state_key(platform, refresh_scope)
    with _refresh_locks_lock:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[key] = lock
        return lock


def _store_refresh_state(
    platform: str,
    state: RefreshState,
    *,
    refresh_scope: str | None = None,
    db_path: Path | None = None,
) -> None:
    now = _utc_now_iso()
    engine = _engine(db_path)
    try:
        with engine.begin() as conn:
            _write_refresh_state(conn, platform, state, now=now, refresh_scope=refresh_scope)
    finally:
        engine.dispose()


def _schedule_refresh(platform: str, *, db_path: Path | None = None, **kwargs: Any) -> bool:
    refresh_scope = _refresh_scope_for(platform, kwargs)
    key = _refresh_state_key(platform, refresh_scope)
    with _scheduled_refreshes_lock:
        if key in _scheduled_refreshes:
            return True
        _scheduled_refreshes.add(key)

    def _run_refresh() -> None:
        try:
            refresh_platform(platform=platform, db_path=db_path, **kwargs)
        finally:
            with _scheduled_refreshes_lock:
                _scheduled_refreshes.discard(key)

    thread = threading.Thread(
        target=_run_refresh,
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _scheduled_refreshes_lock:
            _scheduled_refreshes.discard(key)
        raise
    return True


def _summary(all_chats: list[ChannelInfo], visible_chats: list[ChannelInfo], *, include_private: bool) -> dict[str, int]:
    return {
        "discovered_count": len(all_chats),
        "visible_count": len(visible_chats),
        "not_returned_count": sum(
            1 for chat in all_chats if chat.visibility_status == VISIBILITY_NOT_RETURNED
        ),
        "hidden_private_count": 0 if include_private else sum(1 for chat in all_chats if chat.is_private),
        "forum_count": sum(1 for chat in visible_chats if bool(chat.metadata.get(METADATA_SUPPORTS_TOPICS))),
    }


def _filter_response_chats(
    chats: list[ChannelInfo],
    *,
    include_private: bool,
    require_member: bool,
    include_not_returned: bool = False,
) -> list[ChannelInfo]:
    result = chats
    if not include_private:
        result = [chat for chat in result if not chat.is_private]
    if not include_not_returned:
        # Hide channels that the platform no longer returns (deleted / inaccessible)
        # by default, across all platforms. They remain in storage and are exposed
        # only when a caller explicitly opts in (review / manual remove views).
        result = [chat for chat in result if chat.visibility_status != VISIBILITY_NOT_RETURNED]
    if require_member:
        # Keep only member channels for the Slack member-only view. Do NOT re-drop
        # not_returned here: when the caller opted in (include_not_returned), the
        # stale member rows are exactly what the review/remove view needs; when
        # they did not, those rows were already removed above.
        result = [chat for chat in result if chat.is_member is True]
    return result


def _response_cache_requires_refresh(platform: str, state: RefreshState, refresh_kwargs: dict[str, Any]) -> bool:
    if platform != "slack" or not bool(refresh_kwargs.get("browse_all", False)):
        return False
    return state.last_success_at is None


def _payload_native_type(native_type: str) -> str | int:
    if native_type.isdecimal():
        try:
            return int(native_type)
        except ValueError:
            return native_type
    return native_type


def _rename_preserving_existing(source: Path, target: Path) -> None:
    if not source.exists():
        return
    if not target.exists():
        try:
            source.rename(target)
        except FileNotFoundError:
            return
        return
    try:
        payload = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=target.parent,
        suffix=".migrated",
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    try:
        source.unlink()
    except FileNotFoundError:
        return
    logger.info("Legacy discovered chats file already migrated; preserved duplicate at %s", temp_path)
