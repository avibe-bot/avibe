"""Centralized authorization pipeline.

Every IM entry point calls ``check_auth`` before dispatching to handlers.
This eliminates duplicated inline auth checks across 8 separate code paths.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.v2_settings import SettingsStore

# ---------------------------------------------------------------------------
# Actions that require admin permission
# ---------------------------------------------------------------------------
ADMIN_PROTECTED_ACTIONS: frozenset = frozenset(
    {
        # Button / callback_data callbacks
        "cmd_settings",
        "cmd_routing",
        "cmd_change_cwd",
        "auth_setup",
        "vibe_update_now",
        # Feishu form submit button names
        "cwd_submit",
        "settings_submit",
        "routing_backend_select",
        "routing_submit",
        # Text commands (used with _admin_guard)
        "setup",
        "set_cwd",
        "settings",
    }
)

# Commands exempt from DM bind gate (unbound users can use these)
BIND_EXEMPT_COMMANDS: frozenset = frozenset({"bind"})


def _is_admin_protected(action: str) -> bool:
    """Check if an action requires admin permission.

    Handles both exact matches and prefix matches (e.g. ``vibe_update_now:v1.2``).
    """
    if action in ADMIN_PROTECTED_ACTIONS:
        return True
    # Prefix match for actions with dynamic suffixes
    if action.startswith("vibe_update_now"):
        return True
    if action.startswith("auth_setup"):
        return True
    return False


def _is_enabled_user(store: object, user_id: str, platform: str | None) -> bool:
    if hasattr(store, "is_enabled_user"):
        try:
            return store.is_enabled_user(user_id, platform=platform)
        except TypeError:
            return store.is_enabled_user(user_id)
    try:
        return store.is_bound_user(user_id, platform=platform)
    except TypeError:
        return store.is_bound_user(user_id)


@dataclass
class AuthResult:
    """Result of an authorization check."""

    allowed: bool
    denial: str = ""  # "" | "unbound_dm" | "unauthorized_channel" | "not_admin"
    is_dm: bool = False


def check_auth(
    *,
    user_id: str,
    channel_id: str,
    is_dm: bool,
    platform: str | None = None,
    action: str = "",
    settings_manager: object | None = None,
    store: "SettingsStore | None" = None,
) -> AuthResult:
    """Run the centralized authorization pipeline.

    Order of checks:
        1. DM bind gate  — DM users must be bound (except ``bind`` / ``/bind``)
        2. Channel auth   — channel messages must come from enabled channels
        3. Admin check    — protected actions require admin

    Parameters
    ----------
    user_id:     The actor's platform user ID.
    channel_id:  The channel / chat ID.
    is_dm:       Whether this is a direct-message context.
    action:      The action being performed (command name, callback_data,
                 or Feishu form button_name).  Used for admin check and
                 bind-gate exemption.
    store:       ``SettingsStore`` instance for permission lookups.
                 When *None* (no settings configured), everything is allowed.
    """
    if store is None and settings_manager is not None and hasattr(settings_manager, "get_store"):
        try:
            store = settings_manager.get_store()
        except Exception:
            store = None

    if store is None:
        return AuthResult(allowed=True, is_dm=is_dm)

    # Ensure we have fresh data
    store.maybe_reload()

    # 1. DM bind gate
    if is_dm:
        if action in BIND_EXEMPT_COMMANDS:
            return AuthResult(allowed=True, is_dm=True)
        if not _is_enabled_user(store, user_id, platform):
            return AuthResult(allowed=False, denial="unbound_dm", is_dm=True)
        # DM users skip channel authorization
    else:
        # 2. Channel authorization
        if hasattr(store, "find_channel"):
            try:
                ch = store.find_channel(channel_id, platform=platform)
            except TypeError:
                ch = store.find_channel(channel_id)
        else:
            ch = getattr(getattr(store, "settings", None), "channels", {}).get(channel_id)
        if not ch or not ch.enabled:
            return AuthResult(allowed=False, denial="unauthorized_channel", is_dm=False)

        # 2b. Optional per-channel require_bind gate: when enabled, only bound
        # users may drive the agent in this channel. Unbound senders are denied
        # with a denial type that maps to no message (silent ignore), so other
        # members' chatter does not trigger the bot or spam the channel.
        if getattr(ch, "require_bind", None):
            if not _is_enabled_user(store, user_id, platform):
                return AuthResult(allowed=False, denial="not_bound_channel", is_dm=False)

    # 3. Admin check for protected actions
    if _is_admin_protected(action):
        try:
            has_admin = store.has_any_admin(platform=platform)
        except TypeError:
            has_admin = store.has_any_admin()
        try:
            is_admin = store.is_admin(user_id, platform=platform)
        except TypeError:
            is_admin = store.is_admin(user_id)
        if has_admin and not is_admin:
            return AuthResult(allowed=False, denial="not_admin", is_dm=is_dm)

    return AuthResult(allowed=True, is_dm=is_dm)
