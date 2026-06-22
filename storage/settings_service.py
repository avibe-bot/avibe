from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config.v2_settings import (
    BindCode,
    ChannelSettings,
    GuildSettings,
    RoutingSettings,
    SettingsState,
    UserSettings,
    _make_scoped_key,
    _split_scoped_key,
    normalize_routing_settings,
    normalize_show_message_types,
)
from storage.db import SqliteInvalidationProbe, create_sqlite_engine
from storage.models import auth_codes, scope_settings, scopes

SETTINGS_VERSION = 1
GUILD_POLICY_KIND = "guild_policy"
# Scope types whose settings this store owns. avibe project scopes
# (storage.projects_service) share the scope_settings table but are NOT managed
# here, so save_state must never delete or overwrite their rows.
_MANAGED_SCOPE_TYPES = ("channel", "platform", "guild", "user")


class SQLiteSettingsService:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.engine = create_sqlite_engine(db_path)
        self._probe = SqliteInvalidationProbe(self.engine)

    def close(self) -> None:
        self._probe.close()
        self.engine.dispose()

    def has_external_write(self) -> bool:
        return self._probe.has_external_write()

    def load_state(self) -> SettingsState:
        with self.engine.connect() as conn:
            return SettingsState(
                channels=self._load_channels(conn),
                guilds=self._load_guilds(conn),
                guild_scope_platforms=self._load_guild_scope_platforms(conn),
                guild_default_enabled=self._load_guild_policies(conn),
                users=self._load_users(conn),
                bind_codes=self._load_bind_codes(conn),
            )

    def save_state(self, state: SettingsState) -> None:
        with self.engine.begin() as conn:
            now = _utc_now_iso()
            # Per-row reconcile (NOT a delete-everything rewrite): upsert each
            # managed scope's settings, then delete only the managed rows that
            # vanished from the state. avibe project scopes share this table but
            # are owned by projects_service, so they are never touched here — the
            # old full-table clear wiped their workdir.
            kept: set[str] = set()

            for scoped_key, item in state.channels.items():
                platform, channel_id = _split_scoped_key(scoped_key)
                scope_id = upsert_scope(conn, platform or "unknown", "channel", channel_id, now=now)
                routing = asdict(item.routing)
                self._upsert_scope_settings(
                    conn,
                    scope_id=scope_id,
                    enabled=_bool_int(item.enabled),
                    role=None,
                    workdir=item.custom_cwd,
                    require_mention=_nullable_bool_int(item.require_mention),
                    settings_json=_json_dumps(
                        {
                            "show_message_types": normalize_show_message_types(item.show_message_types),
                            "routing": routing,
                            "require_bind": item.require_bind,
                        }
                    ),
                    created_at=now,
                    updated_at=now,
                    settings_version=SETTINGS_VERSION,
                    **_routing_columns(item.routing),
                )
                kept.add(scope_id)

            for platform in sorted(state.guild_scope_platforms):
                scope_id = upsert_scope(conn, platform, "platform", platform, now=now)
                self._upsert_scope_settings(
                    conn,
                    scope_id=scope_id,
                    enabled=_bool_int(state.guild_default_enabled.get(platform, False)),
                    role=None,
                    workdir=None,
                    agent_name=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=SETTINGS_VERSION,
                    settings_json=_json_dumps({"kind": GUILD_POLICY_KIND}),
                    created_at=now,
                    updated_at=now,
                )
                kept.add(scope_id)

            for scoped_key, item in state.guilds.items():
                platform, guild_id = _split_scoped_key(scoped_key)
                scope_id = upsert_scope(conn, platform or "discord", "guild", guild_id, now=now)
                self._upsert_scope_settings(
                    conn,
                    scope_id=scope_id,
                    enabled=_bool_int(item.enabled),
                    role=None,
                    workdir=None,
                    agent_name=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=SETTINGS_VERSION,
                    settings_json=_json_dumps({}),
                    created_at=now,
                    updated_at=now,
                )
                kept.add(scope_id)

            for scoped_key, item in state.users.items():
                platform, user_id = _split_scoped_key(scoped_key)
                scope_id = upsert_scope(
                    conn,
                    platform or "unknown",
                    "user",
                    user_id,
                    display_name=item.display_name,
                    is_private=True,
                    now=now,
                )
                routing = asdict(item.routing)
                self._upsert_scope_settings(
                    conn,
                    scope_id=scope_id,
                    enabled=_bool_int(item.enabled),
                    role="admin" if item.is_admin else "member",
                    workdir=item.custom_cwd,
                    require_mention=None,
                    settings_version=SETTINGS_VERSION,
                    settings_json=_json_dumps(
                        {
                            "bound_at": item.bound_at or "",
                            "dm_chat_id": item.dm_chat_id or "",
                            "pending_bind_menu_hint": bool(item.pending_bind_menu_hint),
                            "show_message_types": normalize_show_message_types(item.show_message_types),
                            "routing": routing,
                        }
                    ),
                    created_at=now,
                    updated_at=now,
                    **_routing_columns(item.routing),
                )
                kept.add(scope_id)

            self._delete_removed_scope_settings(conn, kept)
            self._sync_bind_codes(conn, state.bind_codes, now)

    def _upsert_scope_settings(self, conn: Connection, *, scope_id: str, **values: Any) -> None:
        """Insert or update one scope's settings row by scope_id — no delete.

        On conflict every provided column is overwritten except ``created_at``
        (the original creation time is kept).
        """
        stmt = sqlite_insert(scope_settings).values(scope_id=scope_id, **values)
        update_set = {col: getattr(stmt.excluded, col) for col in values if col != "created_at"}
        conn.execute(
            stmt.on_conflict_do_update(index_elements=[scope_settings.c.scope_id], set_=update_set)
        )

    def _delete_removed_scope_settings(self, conn: Connection, kept: set[str]) -> None:
        """Delete settings rows for managed scopes no longer present in the state.

        Scoped to ``_MANAGED_SCOPE_TYPES`` so avibe project scopes (owned by
        projects_service) keep their settings/workdir even though they live in
        the same table.
        """
        managed_scope_ids = select(scopes.c.id).where(scopes.c.scope_type.in_(_MANAGED_SCOPE_TYPES))
        stmt = scope_settings.delete().where(scope_settings.c.scope_id.in_(managed_scope_ids))
        if kept:
            stmt = stmt.where(scope_settings.c.scope_id.notin_(kept))
        conn.execute(stmt)

    def _sync_bind_codes(self, conn: Connection, bind_codes: list[BindCode], now: str) -> None:
        """Upsert the current bind codes and delete the ones that were removed."""
        existing = {row[0] for row in conn.execute(select(auth_codes.c.code))}
        kept: set[str] = set()
        for item in bind_codes:
            kept.add(item.code)
            stmt = sqlite_insert(auth_codes).values(
                code=item.code,
                type=item.type,
                is_active=_bool_int(item.is_active),
                expires_at=item.expires_at,
                used_by_json=_json_dumps(item.used_by),
                created_at=item.created_at or now,
                updated_at=now,
            )
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[auth_codes.c.code],
                    set_={
                        "type": stmt.excluded.type,
                        "is_active": stmt.excluded.is_active,
                        "expires_at": stmt.excluded.expires_at,
                        "used_by_json": stmt.excluded.used_by_json,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
        removed = existing - kept
        if removed:
            conn.execute(auth_codes.delete().where(auth_codes.c.code.in_(removed)))

    def _load_channels(self, conn: Connection) -> dict[str, ChannelSettings]:
        rows = _settings_rows(conn, "channel")
        result: dict[str, ChannelSettings] = {}
        for row in rows:
            payload = _json_loads(row["settings_json"], {})
            key = _make_scoped_key(str(row["platform"]), str(row["native_id"]))
            result[key] = ChannelSettings(
                enabled=bool(row["enabled"]),
                show_message_types=normalize_show_message_types(_json_list(payload.get("show_message_types"))),
                custom_cwd=row["workdir"],
                routing=_routing_from_row(row, payload),
                require_mention=_nullable_bool(row["require_mention"]),
                require_bind=payload.get("require_bind"),
            )
        return result

    def _load_guilds(self, conn: Connection) -> dict[str, GuildSettings]:
        rows = _settings_rows(conn, "guild")
        result: dict[str, GuildSettings] = {}
        for row in rows:
            key = _make_scoped_key(str(row["platform"]), str(row["native_id"]))
            result[key] = GuildSettings(enabled=bool(row["enabled"]))
        return result

    def _load_guild_scope_platforms(self, conn: Connection) -> set[str]:
        return set(self._load_guild_policies(conn).keys())

    def _load_guild_policies(self, conn: Connection) -> dict[str, bool]:
        rows = _settings_rows(conn, "platform")
        result: dict[str, bool] = {}
        for row in rows:
            payload = _json_loads(row["settings_json"], {})
            if payload.get("kind") == GUILD_POLICY_KIND:
                result[str(row["platform"])] = bool(row["enabled"])
        return result

    def _load_users(self, conn: Connection) -> dict[str, UserSettings]:
        rows = _settings_rows(conn, "user")
        result: dict[str, UserSettings] = {}
        for row in rows:
            payload = _json_loads(row["settings_json"], {})
            key = _make_scoped_key(str(row["platform"]), str(row["native_id"]))
            result[key] = UserSettings(
                display_name=row["display_name"] or "",
                is_admin=str(row["role"] or "").lower() in {"admin", "owner"},
                bound_at=str(payload.get("bound_at") or ""),
                enabled=bool(row["enabled"]),
                show_message_types=normalize_show_message_types(_json_list(payload.get("show_message_types"))),
                custom_cwd=row["workdir"],
                routing=_routing_from_row(row, payload),
                dm_chat_id=str(payload.get("dm_chat_id") or ""),
                pending_bind_menu_hint=bool(payload.get("pending_bind_menu_hint", False)),
            )
        return result

    def _load_bind_codes(self, conn: Connection) -> list[BindCode]:
        rows = conn.execute(select(auth_codes)).mappings()
        return [
            BindCode(
                code=row["code"],
                type=row["type"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                is_active=bool(row["is_active"]),
                used_by=_json_loads(row["used_by_json"], []),
            )
            for row in rows
        ]


def upsert_scope(
    conn: Connection,
    platform: str,
    scope_type: str,
    native_id: str,
    *,
    now: str,
    parent_scope_id: str | None = None,
    display_name: str | None = None,
    native_type: str | None = None,
    is_private: bool | None = None,
    supports_threads: bool | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    scope_id = make_scope_id(platform, scope_type, native_id)
    existing = conn.execute(select(scopes.c.id).where(scopes.c.id == scope_id)).scalar_one_or_none()
    values = {
        "parent_scope_id": parent_scope_id,
        "display_name": display_name,
        "native_type": native_type,
        "last_seen_at": now,
        "updated_at": now,
    }
    if is_private is not None:
        values["is_private"] = _bool_int(is_private)
    if supports_threads is not None:
        values["supports_threads"] = _bool_int(supports_threads)
    if metadata is not None:
        values["metadata_json"] = _json_dumps(metadata)
    if existing is not None:
        clean_values = {key: value for key, value in values.items() if value not in (None, "", _json_dumps({}))}
        if clean_values:
            conn.execute(scopes.update().where(scopes.c.id == scope_id).values(**clean_values))
        return scope_id

    insert_values = {
        "id": scope_id,
        "platform": platform,
        "scope_type": scope_type,
        "native_id": native_id,
        "is_private": _bool_int(is_private),
        "supports_threads": _bool_int(supports_threads),
        "metadata_json": _json_dumps(metadata or {}),
        "first_seen_at": now,
        **values,
    }
    conn.execute(scopes.insert().values(**insert_values))
    return scope_id


def make_scope_id(platform: str, scope_type: str, native_id: str) -> str:
    return f"{platform}::{scope_type}::{native_id}"


def _settings_rows(conn: Connection, scope_type: str):
    return conn.execute(
        select(
            scopes.c.id.label("scope_id"),
            scopes.c.platform,
            scopes.c.scope_type,
            scopes.c.native_id,
            scopes.c.display_name,
            scope_settings,
        )
        .join(scope_settings, scope_settings.c.scope_id == scopes.c.id)
        .where(scopes.c.scope_type == scope_type)
    ).mappings()


def _routing_columns(routing: RoutingSettings) -> dict[str, str | None]:
    routing = normalize_routing_settings(routing)
    model = routing.model
    effort = routing.reasoning_effort
    variant = _active_routing_variant(routing)
    return {
        "agent_name": routing.agent_name,
        "agent_variant": variant,
        "model": model,
        "reasoning_effort": effort,
    }


def _active_routing_variant(routing: RoutingSettings) -> str | None:
    agent_name = routing.agent_name
    if agent_name == "codex":
        return routing.codex_agent
    if agent_name == "claude":
        return routing.claude_agent
    if agent_name == "opencode":
        return routing.opencode_agent
    return routing.codex_agent or routing.claude_agent or routing.opencode_agent


def _routing_from_row(row: dict[str, Any], payload: dict[str, Any]) -> RoutingSettings:
    routing_payload = payload.get("routing") or {}
    routing = RoutingSettings(
        agent_name=routing_payload.get("agent_name") or routing_payload.get("agent"),
        model=routing_payload.get("model") or routing_payload.get("model_override"),
        reasoning_effort=routing_payload.get("reasoning_effort") or routing_payload.get("reasoning_effort_override"),
        opencode_agent=routing_payload.get("opencode_agent"),
        opencode_model=routing_payload.get("opencode_model"),
        opencode_reasoning_effort=routing_payload.get("opencode_reasoning_effort"),
        claude_agent=routing_payload.get("claude_agent"),
        claude_model=routing_payload.get("claude_model"),
        claude_reasoning_effort=routing_payload.get("claude_reasoning_effort"),
        codex_agent=routing_payload.get("codex_agent"),
        codex_model=routing_payload.get("codex_model"),
        codex_reasoning_effort=routing_payload.get("codex_reasoning_effort"),
    )
    stored_agent_name = row.get("agent_name")
    if stored_agent_name:
        routing.agent_name = routing.agent_name or str(stored_agent_name)
    variant = row.get("agent_variant")
    model = row.get("model")
    effort = row.get("reasoning_effort")
    if routing.agent_name:
        routing.model = routing.model or model
        routing.reasoning_effort = routing.reasoning_effort or effort
    if routing.agent_name == "codex":
        routing.codex_agent = routing.codex_agent or variant
    elif routing.agent_name == "claude":
        routing.claude_agent = routing.claude_agent or variant
    elif routing.agent_name == "opencode":
        routing.opencode_agent = routing.opencode_agent or variant
    return routing


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def _nullable_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _nullable_bool_int(value: Any) -> int | None:
    return None if value is None else _bool_int(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
