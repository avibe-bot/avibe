import json
import logging
import secrets
import string
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import paths

logger = logging.getLogger(__name__)

DEFAULT_SHOW_MESSAGE_TYPES: List[str] = ["assistant"]
# "system" is deprecated: system/init messages are never pushed to users, so it
# is no longer a user-facing toggle. Normalizing against this set drops any
# legacy "system" value still stored in show_message_types.
ALLOWED_MESSAGE_TYPES = {"assistant", "toolcall"}
SCHEMA_VERSION = 5
SCOPED_KEY_SEP = "::"

# Bind code prefix and length
_BIND_CODE_PREFIX = "vr-"
_BIND_CODE_RANDOM_LENGTH = 6
_BIND_CODE_ALPHABET = string.ascii_lowercase + string.digits


def normalize_show_message_types(show_message_types: Optional[List[str]]) -> List[str]:
    if show_message_types is None:
        return DEFAULT_SHOW_MESSAGE_TYPES.copy()
    return [msg for msg in show_message_types if msg in ALLOWED_MESSAGE_TYPES]


def _generate_bind_code() -> str:
    """Generate a random bind code like 'vr-a3x9k2'."""
    random_part = "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(_BIND_CODE_RANDOM_LENGTH))
    return f"{_BIND_CODE_PREFIX}{random_part}"


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _make_scoped_key(platform: str, item_id: str) -> str:
    return f"{platform}{SCOPED_KEY_SEP}{item_id}"


def _split_scoped_key(scoped_key: str) -> Tuple[Optional[str], str]:
    if SCOPED_KEY_SEP in scoped_key:
        platform, raw_id = scoped_key.split(SCOPED_KEY_SEP, 1)
        if platform:
            return platform, raw_id
    return None, scoped_key


def _infer_channel_platform(channel_id: str) -> str:
    cid = str(channel_id)
    if cid.startswith("oc_"):
        return "lark"
    if cid and cid[0] in {"C", "G", "D"}:
        return "slack"
    if cid.isdigit() and len(cid) >= 15:
        return "discord"
    return "unknown"


def _infer_user_platform(user_id: str) -> str:
    uid = str(user_id)
    if uid.startswith("ou_"):
        return "lark"
    if uid and uid[0] in {"U", "W"}:
        return "slack"
    if uid.isdigit() and len(uid) >= 15:
        return "discord"
    return "unknown"


@dataclass
class RoutingSettings:
    agent_name: Optional[str] = None
    # Scope-level overrides applied on top of the selected Vibe Agent.
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    # OpenCode settings
    opencode_agent: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_reasoning_effort: Optional[str] = None
    # Claude Code settings
    claude_agent: Optional[str] = None
    claude_model: Optional[str] = None
    claude_reasoning_effort: Optional[str] = None
    # Codex settings
    codex_agent: Optional[str] = None
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None

def _backend_specific_value(routing: RoutingSettings, backend: Optional[str], field: str) -> Optional[str]:
    if backend not in {"opencode", "claude", "codex"}:
        return None
    return getattr(routing, f"{backend}_{field}", None)


def _payload_value(payload: dict, key: str, fallback_key: str) -> Optional[str]:
    if key in payload:
        return payload.get(key)
    return payload.get(fallback_key)


def normalize_routing_settings(routing: Optional[RoutingSettings]) -> RoutingSettings:
    """Normalize scope routing without consulting deprecated backend routing."""
    if routing is None:
        return RoutingSettings()
    agent_name = getattr(routing, "agent_name", None)
    builtin_agent_backend = agent_name if agent_name in {"opencode", "claude", "codex"} else None
    model = getattr(routing, "model", None)
    reasoning_effort = getattr(routing, "reasoning_effort", None)
    return RoutingSettings(
        agent_name=agent_name,
        model=model or _backend_specific_value(routing, builtin_agent_backend, "model"),
        reasoning_effort=reasoning_effort
        or _backend_specific_value(routing, builtin_agent_backend, "reasoning_effort"),
        opencode_agent=getattr(routing, "opencode_agent", None),
        opencode_model=getattr(routing, "opencode_model", None),
        opencode_reasoning_effort=getattr(routing, "opencode_reasoning_effort", None),
        claude_agent=getattr(routing, "claude_agent", None),
        claude_model=getattr(routing, "claude_model", None),
        claude_reasoning_effort=getattr(routing, "claude_reasoning_effort", None),
        codex_agent=getattr(routing, "codex_agent", None),
        codex_model=getattr(routing, "codex_model", None),
        codex_reasoning_effort=getattr(routing, "codex_reasoning_effort", None),
    )


def routing_model_for_backend(routing: Optional[RoutingSettings], backend: Optional[str]) -> Optional[str]:
    normalized = normalize_routing_settings(routing)
    if not _routing_backend_matches(normalized, backend):
        return None
    return normalized.model


def routing_reasoning_effort_for_backend(
    routing: Optional[RoutingSettings],
    backend: Optional[str],
) -> Optional[str]:
    normalized = normalize_routing_settings(routing)
    if not _routing_backend_matches(normalized, backend):
        return None
    return normalized.reasoning_effort


def _routing_backend_matches(routing: RoutingSettings, backend: Optional[str]) -> bool:
    agent_name = getattr(routing, "agent_name", None)
    if agent_name in {"opencode", "claude", "codex"}:
        return agent_name == backend
    return True


@dataclass
class ChannelSettings:
    enabled: bool = False
    show_message_types: List[str] = field(default_factory=lambda: DEFAULT_SHOW_MESSAGE_TYPES.copy())
    custom_cwd: Optional[str] = None
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    # Per-channel require_mention override: None=use global default, True=require, False=don't require
    require_mention: Optional[bool] = None
    # Per-channel require_bind gate: None/False=off (any channel member), True=only
    # process messages from bound users; unbound senders are silently ignored.
    require_bind: Optional[bool] = None


@dataclass
class GuildSettings:
    enabled: bool = True


@dataclass
class UserSettings:
    """Settings for a bound DM user."""

    display_name: str = ""
    is_admin: bool = False
    bound_at: str = ""  # ISO 8601 timestamp
    enabled: bool = True
    show_message_types: List[str] = field(default_factory=lambda: DEFAULT_SHOW_MESSAGE_TYPES.copy())
    custom_cwd: Optional[str] = None
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    dm_chat_id: str = ""
    pending_bind_menu_hint: bool = False


@dataclass
class BindCode:
    """A bind code for authorizing DM access."""

    code: str
    type: str  # "one_time" or "expiring"
    created_at: str  # ISO 8601
    expires_at: Optional[str] = None  # ISO 8601, only for "expiring" type
    is_active: bool = True
    used_by: List[str] = field(default_factory=list)  # user_ids that used this code


@dataclass
class SettingsState:
    channels: Dict[str, ChannelSettings] = field(default_factory=dict)
    guilds: Dict[str, GuildSettings] = field(default_factory=dict)
    guild_scope_platforms: set[str] = field(default_factory=set)
    guild_default_enabled: Dict[str, bool] = field(default_factory=dict)
    users: Dict[str, UserSettings] = field(default_factory=dict)
    bind_codes: List[BindCode] = field(default_factory=list)


def _parse_routing(payload: dict) -> RoutingSettings:
    """Parse a routing settings dict into a RoutingSettings dataclass."""
    if not isinstance(payload, dict):
        payload = {}
    model_key_present = "model" in payload or "model_override" in payload
    reasoning_key_present = "reasoning_effort" in payload or "reasoning_effort_override" in payload
    return normalize_routing_settings(
        RoutingSettings(
            agent_name=payload.get("agent_name") or payload.get("agent"),
            model=_payload_value(payload, "model", "model_override"),
            reasoning_effort=_payload_value(payload, "reasoning_effort", "reasoning_effort_override"),
            opencode_agent=payload.get("opencode_agent"),
            opencode_model=None if model_key_present else payload.get("opencode_model"),
            opencode_reasoning_effort=None if reasoning_key_present else payload.get("opencode_reasoning_effort"),
            claude_agent=payload.get("claude_agent"),
            claude_model=None if model_key_present else payload.get("claude_model"),
            claude_reasoning_effort=None if reasoning_key_present else payload.get("claude_reasoning_effort"),
            codex_agent=payload.get("codex_agent"),
            codex_model=None if model_key_present else payload.get("codex_model"),
            codex_reasoning_effort=None if reasoning_key_present else payload.get("codex_reasoning_effort"),
        )
    )


def _routing_to_dict(routing: RoutingSettings) -> dict:
    """Serialize a RoutingSettings to dict."""
    routing = normalize_routing_settings(routing)
    return {
        "agent_name": routing.agent_name,
        "model": routing.model,
        "reasoning_effort": routing.reasoning_effort,
        "opencode_agent": routing.opencode_agent,
        "opencode_model": routing.opencode_model,
        "opencode_reasoning_effort": routing.opencode_reasoning_effort,
        "claude_agent": routing.claude_agent,
        "claude_model": routing.claude_model,
        "claude_reasoning_effort": routing.claude_reasoning_effort,
        "codex_agent": routing.codex_agent,
        "codex_model": routing.codex_model,
        "codex_reasoning_effort": routing.codex_reasoning_effort,
    }


def routing_to_compat_dict(routing: RoutingSettings) -> dict:
    """Serialize routing with legacy read-only aliases derived from canonical fields."""
    routing = normalize_routing_settings(routing)
    return _routing_to_dict(routing)


def parse_settings_payload(payload: dict) -> tuple[SettingsState, bool]:
    """Parse current or legacy settings JSON into normalized SettingsState."""
    if not isinstance(payload, dict):
        raise ValueError("settings payload must be an object")

    channels: Dict[str, ChannelSettings] = {}
    guilds: Dict[str, GuildSettings] = {}
    guild_scope_platforms: set[str] = set()
    guild_default_enabled: Dict[str, bool] = {}
    users: Dict[str, UserSettings] = {}
    migrated_legacy_channels = False

    scopes = payload.get("scopes")
    if isinstance(scopes, dict):
        raw_channel_scopes = scopes.get("channel") or {}
        if isinstance(raw_channel_scopes, dict):
            for platform, items in raw_channel_scopes.items():
                if not isinstance(items, dict):
                    continue
                for channel_id, cp in items.items():
                    if not isinstance(cp, dict):
                        continue
                    key = _make_scoped_key(str(platform), str(channel_id))
                    channels[key] = ChannelSettings(
                        enabled=cp.get("enabled", False),
                        show_message_types=normalize_show_message_types(cp.get("show_message_types")),
                        custom_cwd=cp.get("custom_cwd"),
                        routing=_parse_routing(cp.get("routing") or {}),
                        require_mention=cp.get("require_mention"),
                        require_bind=cp.get("require_bind"),
                    )

        raw_guild_scopes = scopes.get("guild") or {}
        if isinstance(raw_guild_scopes, dict):
            raw_guild_policy = scopes.get("guild_policy") or {}
            for platform, items in raw_guild_scopes.items():
                platform_key = str(platform)
                guild_scope_platforms.add(platform_key)
                policy = raw_guild_policy.get(platform_key) if isinstance(raw_guild_policy, dict) else None
                if isinstance(policy, dict):
                    guild_default_enabled[platform_key] = bool(policy.get("default_enabled", False))
                else:
                    guild_default_enabled[platform_key] = False
                if not isinstance(items, dict):
                    continue
                for guild_id, gp in items.items():
                    if not isinstance(gp, dict):
                        continue
                    key = _make_scoped_key(platform_key, str(guild_id))
                    guilds[key] = GuildSettings(enabled=gp.get("enabled", True))

        raw_user_scopes = scopes.get("user") or {}
        if isinstance(raw_user_scopes, dict):
            for platform, items in raw_user_scopes.items():
                if not isinstance(items, dict):
                    continue
                for user_id, up in items.items():
                    if not isinstance(up, dict):
                        continue
                    key = _make_scoped_key(str(platform), str(user_id))
                    users[key] = UserSettings(
                        display_name=up.get("display_name", ""),
                        is_admin=up.get("is_admin", False),
                        bound_at=up.get("bound_at", ""),
                        enabled=up.get("enabled", True),
                        show_message_types=normalize_show_message_types(up.get("show_message_types")),
                        custom_cwd=up.get("custom_cwd"),
                        routing=_parse_routing(up.get("routing") or {}),
                        dm_chat_id=up.get("dm_chat_id", ""),
                        pending_bind_menu_hint=bool(up.get("pending_bind_menu_hint", False)),
                    )
    else:
        raw_channels = payload.get("channels") or {}
        if not isinstance(raw_channels, dict):
            logger.error("Failed to load settings: channels must be an object")
            raw_channels = {}
        for channel_id, cp in raw_channels.items():
            if not isinstance(cp, dict):
                continue
            platform = _infer_channel_platform(str(channel_id))
            scoped_key = _make_scoped_key(platform, str(channel_id))
            channels[scoped_key] = ChannelSettings(
                enabled=cp.get("enabled", False),
                show_message_types=normalize_show_message_types(cp.get("show_message_types")),
                custom_cwd=cp.get("custom_cwd"),
                routing=_parse_routing(cp.get("routing") or {}),
                require_mention=cp.get("require_mention"),
                require_bind=cp.get("require_bind"),
            )
            migrated_legacy_channels = True

        raw_users = payload.get("users") or {}
        if isinstance(raw_users, dict):
            for user_id, up in raw_users.items():
                if not isinstance(up, dict):
                    continue
                platform = _infer_user_platform(str(user_id))
                scoped_key = _make_scoped_key(platform, str(user_id))
                users[scoped_key] = UserSettings(
                    display_name=up.get("display_name", ""),
                    is_admin=up.get("is_admin", False),
                    bound_at=up.get("bound_at", ""),
                    enabled=up.get("enabled", True),
                    show_message_types=normalize_show_message_types(up.get("show_message_types")),
                    custom_cwd=up.get("custom_cwd"),
                    routing=_parse_routing(up.get("routing") or {}),
                    dm_chat_id=up.get("dm_chat_id", ""),
                    pending_bind_menu_hint=bool(up.get("pending_bind_menu_hint", False)),
                )

    raw_codes = payload.get("bind_codes") or []
    bind_codes: List[BindCode] = []
    if isinstance(raw_codes, list):
        for bc in raw_codes:
            if not isinstance(bc, dict):
                continue
            bind_codes.append(
                BindCode(
                    code=bc.get("code", ""),
                    type=bc.get("type", "one_time"),
                    created_at=bc.get("created_at", ""),
                    expires_at=bc.get("expires_at"),
                    is_active=bc.get("is_active", True),
                    used_by=bc.get("used_by") or [],
                )
            )

    return (
        SettingsState(
            channels=channels,
            guilds=guilds,
            guild_scope_platforms=guild_scope_platforms,
            guild_default_enabled=guild_default_enabled,
            users=users,
            bind_codes=bind_codes,
        ),
        migrated_legacy_channels,
    )


def load_settings_state_from_json(settings_path: Path) -> tuple[SettingsState, bool]:
    if not settings_path.exists():
        return SettingsState(), False
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    return parse_settings_payload(payload)


class SettingsStore:
    # ------------------------------------------------------------------
    # Singleton: one store shared by bot process AND UI API handlers.
    # ------------------------------------------------------------------
    _instance: Optional["SettingsStore"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, settings_path: Optional[Path] = None) -> "SettingsStore":
        """Return the process-wide singleton, creating it on first call.

        Automatically reloads from disk if the file has changed.
        """
        with cls._instance_lock:
            target_path = settings_path or paths.get_settings_path()
            if cls._instance is None or cls._instance.settings_path != target_path:
                cls._instance = cls(target_path)
            else:
                cls._instance.maybe_reload()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for tests only)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.close()
            cls._instance = None

    def __init__(self, settings_path: Optional[Path] = None):
        self.settings_path = Path(settings_path) if settings_path else paths.get_settings_path()
        self.db_path = self.settings_path.with_name("vibe.sqlite")
        self.settings: SettingsState = SettingsState()
        self._bind_lock = threading.Lock()  # Guards atomic bind operations
        self._file_mtime: float = 0
        from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config
        from storage.settings_service import SQLiteSettingsService

        ensure_sqlite_state(
            db_path=self.db_path,
            state_dir=self.settings_path.parent,
            primary_platform=resolve_primary_platform_from_config(self.settings_path.parent),
        )
        self._service = SQLiteSettingsService(self.db_path)
        self._load()
        self._service.has_external_write()

    def close(self) -> None:
        service = getattr(self, "_service", None)
        if service is not None:
            service.close()

    def maybe_reload(self) -> None:
        """Reload from SQLite if another connection has committed changes."""
        if self._service.has_external_write():
            self._load()

    def _load(self) -> None:
        self.settings = self._service.load_state()
        self._file_mtime += 1

    def save(self) -> None:
        self._service.save_state(self.settings)
        self._service.has_external_write()
        self._file_mtime += 1

    # --- Channel helpers ---

    def _channel_key(self, channel_id: str, platform: Optional[str] = None) -> str:
        return _make_scoped_key(platform, channel_id) if platform else channel_id

    def _user_key(self, user_id: str, platform: Optional[str] = None) -> str:
        return _make_scoped_key(platform, user_id) if platform else user_id

    def get_channels_for_platform(self, platform: str) -> Dict[str, ChannelSettings]:
        result: Dict[str, ChannelSettings] = {}
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        for key, settings in self.settings.channels.items():
            if key.startswith(prefix):
                result[key[len(prefix) :]] = settings
        return result

    def set_channels_for_platform(self, platform: str, channels: Dict[str, ChannelSettings]) -> None:
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        self.settings.channels = {k: v for k, v in self.settings.channels.items() if not k.startswith(prefix)}
        for channel_id, settings in channels.items():
            self.settings.channels[self._channel_key(str(channel_id), platform)] = settings

    def get_guilds_for_platform(self, platform: str) -> Dict[str, GuildSettings]:
        result: Dict[str, GuildSettings] = {}
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        for key, settings in self.settings.guilds.items():
            if key.startswith(prefix):
                result[key[len(prefix) :]] = settings
        return result

    def set_guilds_for_platform(
        self,
        platform: str,
        guilds: Dict[str, GuildSettings],
        default_enabled: bool = False,
    ) -> None:
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        self.settings.guilds = {k: v for k, v in self.settings.guilds.items() if not k.startswith(prefix)}
        self.settings.guild_scope_platforms.add(platform)
        self.settings.guild_default_enabled[platform] = bool(default_enabled)
        for guild_id, settings in guilds.items():
            self.settings.guilds[_make_scoped_key(platform, str(guild_id))] = settings

    def has_guild_scope_for_platform(self, platform: str) -> bool:
        return platform in self.settings.guild_scope_platforms

    def get_guild_default_enabled_for_platform(self, platform: str) -> bool:
        return bool(self.settings.guild_default_enabled.get(platform, False))

    def is_guild_enabled(self, platform: str, guild_id: str) -> bool:
        if not self.has_guild_scope_for_platform(platform):
            return True
        settings = self.settings.guilds.get(_make_scoped_key(platform, str(guild_id)))
        if settings is not None:
            return bool(settings.enabled)
        return self.get_guild_default_enabled_for_platform(platform)

    def get_users_for_platform(self, platform: str) -> Dict[str, UserSettings]:
        result: Dict[str, UserSettings] = {}
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        for key, settings in self.settings.users.items():
            if key.startswith(prefix):
                result[key[len(prefix) :]] = settings
        return result

    def set_users_for_platform(self, platform: str, users: Dict[str, UserSettings]) -> None:
        prefix = f"{platform}{SCOPED_KEY_SEP}"
        self.settings.users = {k: v for k, v in self.settings.users.items() if not k.startswith(prefix)}
        for user_id, settings in users.items():
            self.settings.users[self._user_key(str(user_id), platform)] = settings

    def get_channel(self, channel_id: str, platform: Optional[str] = None) -> ChannelSettings:
        key = self._channel_key(channel_id, platform)
        if platform is None and key not in self.settings.channels:
            suffix = f"{SCOPED_KEY_SEP}{channel_id}"
            for scoped_key, settings in self.settings.channels.items():
                if scoped_key.endswith(suffix):
                    return settings
        if key not in self.settings.channels:
            self.settings.channels[key] = ChannelSettings()
        return self.settings.channels[key]

    def find_channel(self, channel_id: str, platform: Optional[str] = None) -> Optional[ChannelSettings]:
        key = self._channel_key(channel_id, platform)
        if key in self.settings.channels:
            return self.settings.channels[key]
        if platform is None:
            suffix = f"{SCOPED_KEY_SEP}{channel_id}"
            for scoped_key, settings in self.settings.channels.items():
                if scoped_key.endswith(suffix):
                    return settings
        return None

    def update_channel(self, channel_id: str, settings: ChannelSettings, platform: Optional[str] = None) -> None:
        key = self._channel_key(channel_id, platform)
        self.settings.channels[key] = settings
        self.save()

    # --- User helpers ---

    def get_user(self, user_id: str, platform: Optional[str] = None) -> Optional[UserSettings]:
        """Get user settings, or None if user is not bound."""
        key = self._user_key(user_id, platform)
        user = self.settings.users.get(key)
        if user is not None or platform:
            return user
        suffix = f"{SCOPED_KEY_SEP}{user_id}"
        for scoped_key, value in self.settings.users.items():
            if scoped_key.endswith(suffix):
                return value
        return None

    def is_bound_user(self, user_id: str, platform: Optional[str] = None) -> bool:
        if platform:
            return self._user_key(user_id, platform) in self.settings.users
        if user_id in self.settings.users:
            return True
        suffix = f"{SCOPED_KEY_SEP}{user_id}"
        return any(key.endswith(suffix) for key in self.settings.users.keys())

    def is_enabled_user(self, user_id: str, platform: Optional[str] = None) -> bool:
        user = self.get_user(user_id, platform=platform)
        return user is not None and user.enabled

    def is_admin(self, user_id: str, platform: Optional[str] = None) -> bool:
        if platform:
            user = self.settings.users.get(self._user_key(user_id, platform))
            return user is not None and user.enabled and user.is_admin
        if user_id in self.settings.users:
            user = self.settings.users[user_id]
            return user.enabled and user.is_admin
        suffix = f"{SCOPED_KEY_SEP}{user_id}"
        for key, value in self.settings.users.items():
            if key.endswith(suffix) and value.enabled:
                return value.is_admin
        return False

    def has_any_admin(self, platform: Optional[str] = None) -> bool:
        """Return True if at least one admin record exists."""
        if platform:
            prefix = f"{platform}{SCOPED_KEY_SEP}"
            return any(u.is_admin for key, u in self.settings.users.items() if key.startswith(prefix))
        return any(u.is_admin for u in self.settings.users.values())

    def has_enabled_admin(self, platform: Optional[str] = None) -> bool:
        """Return True if at least one enabled admin exists."""
        if platform:
            prefix = f"{platform}{SCOPED_KEY_SEP}"
            return any(u.enabled and u.is_admin for key, u in self.settings.users.items() if key.startswith(prefix))
        return any(u.enabled and u.is_admin for u in self.settings.users.values())

    def get_admins(self, platform: Optional[str] = None) -> Dict[str, UserSettings]:
        """Return enabled admin users."""
        if platform:
            prefix = f"{platform}{SCOPED_KEY_SEP}"
            return {
                uid: u
                for uid, u in self.settings.users.items()
                if uid.startswith(prefix) and u.enabled and u.is_admin
            }
        return {uid: u for uid, u in self.settings.users.items() if u.enabled and u.is_admin}

    def add_user(
        self, user_id: str, display_name: str, is_admin: bool = False, platform: Optional[str] = None
    ) -> UserSettings:
        """Add a new bound user. Returns the created UserSettings."""
        user = UserSettings(
            display_name=display_name,
            is_admin=is_admin,
            bound_at=_now_iso(),
            enabled=True,
        )
        self.settings.users[self._user_key(user_id, platform)] = user
        self.save()
        return user

    def bind_user_with_code(
        self, user_id: str, display_name: str, code: str, dm_chat_id: str = "", platform: Optional[str] = None
    ) -> Tuple[bool, bool]:
        """Atomically validate code, create user, and consume code.

        Returns (success, is_admin).
        Thread-safe: uses a lock to prevent concurrent bind races.
        """
        with self._bind_lock:
            # Ensure we have the latest data from disk (UI API may have
            # created bind codes via the same singleton after we last read).
            self.maybe_reload()

            # Check already bound
            if self.is_bound_user(user_id, platform=platform):
                return False, False

            # Validate code
            bc = self.validate_bind_code(code)
            if bc is None:
                return False, False

            # Auto-admin for first user
            is_admin = not self.has_enabled_admin(platform=platform)

            # Create user
            user = UserSettings(
                display_name=display_name,
                is_admin=is_admin,
                bound_at=_now_iso(),
                enabled=True,
                dm_chat_id=dm_chat_id,
            )
            scoped_user_id = self._user_key(user_id, platform)
            self.settings.users[scoped_user_id] = user

            # Consume code
            bc.used_by.append(scoped_user_id)
            if bc.type == "one_time":
                bc.is_active = False

            self.save()
            return True, is_admin

    def update_user(self, user_id: str, settings: UserSettings, platform: Optional[str] = None) -> None:
        self.settings.users[self._user_key(user_id, platform)] = settings
        self.save()

    def remove_user(self, user_id: str, platform: Optional[str] = None) -> bool:
        key = self._user_key(user_id, platform)
        if key in self.settings.users:
            del self.settings.users[key]
            self.save()
            return True
        return False

    def set_admin(self, user_id: str, is_admin: bool, platform: Optional[str] = None) -> bool:
        """Set admin flag for a user. Returns False if user not found."""
        key = self._user_key(user_id, platform)
        user = self.settings.users.get(key)
        if user is None:
            return False
        user.is_admin = is_admin
        self.save()
        return True

    # --- Bind code helpers ---

    def create_bind_code(self, code_type: str = "one_time", expires_at: Optional[str] = None) -> BindCode:
        """Create a new bind code."""
        code = _generate_bind_code()
        bc = BindCode(
            code=code,
            type=code_type,
            created_at=_now_iso(),
            expires_at=expires_at if code_type == "expiring" else None,
            is_active=True,
        )
        self.settings.bind_codes.append(bc)
        self.save()
        return bc

    def validate_bind_code(self, code: str) -> Optional[BindCode]:
        """Validate a bind code. Returns the BindCode if valid, None otherwise."""
        for bc in self.settings.bind_codes:
            if bc.code != code:
                continue
            if not bc.is_active:
                return None
            if bc.type == "expiring" and bc.expires_at:
                try:
                    expires = datetime.fromisoformat(bc.expires_at)
                    # If only a date was provided (no time component), treat as end-of-day
                    if expires.hour == 0 and expires.minute == 0 and expires.second == 0 and "T" not in bc.expires_at:
                        expires = expires.replace(hour=23, minute=59, second=59)
                    # Ensure timezone-aware comparison
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > expires:
                        return None
                except (ValueError, TypeError):
                    # Fail closed: reject codes with unparseable expiration
                    logger.warning("Bind code %s has unparseable expires_at: %s", code, bc.expires_at)
                    return None
            return bc
        return None

    def use_bind_code(self, code: str, user_id: str) -> bool:
        """Mark a bind code as used by a user. Returns True on success."""
        bc = self.validate_bind_code(code)
        if bc is None:
            return False
        bc.used_by.append(user_id)
        if bc.type == "one_time":
            bc.is_active = False
        self.save()
        return True

    def deactivate_bind_code(self, code: str) -> bool:
        """Deactivate a bind code. Returns True if found and deactivated."""
        for bc in self.settings.bind_codes:
            if bc.code == code:
                bc.is_active = False
                self.save()
                return True
        return False

    def get_bind_codes(self) -> List[BindCode]:
        """Return all bind codes."""
        return list(self.settings.bind_codes)
