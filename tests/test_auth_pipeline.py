from types import SimpleNamespace
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.auth import check_auth


class _Store:
    def __init__(self):
        self.reload_calls = 0
        self.settings = SimpleNamespace(channels={})
        self._bound_users = set()
        self._disabled_users = set()
        self._admins = set()

    def maybe_reload(self):
        self.reload_calls += 1

    def is_bound_user(self, user_id: str) -> bool:
        return user_id in self._bound_users

    def is_enabled_user(self, user_id: str) -> bool:
        return user_id in self._bound_users and user_id not in self._disabled_users

    def has_any_admin(self) -> bool:
        return bool(self._admins)

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins and user_id not in self._disabled_users


class _SettingsManager:
    def __init__(self, store):
        self._store = store

    def get_store(self):
        return self._store


def test_check_auth_uses_settings_manager_store():
    store = _Store()
    manager = _SettingsManager(store)

    result = check_auth(
        user_id="U1",
        channel_id="D1",
        is_dm=True,
        action="bind",
        settings_manager=manager,
    )

    assert result.allowed is True
    assert store.reload_calls == 1


def test_dm_bind_gate_denies_unbound_user():
    store = _Store()
    result = check_auth(
        user_id="U2",
        channel_id="D2",
        is_dm=True,
        action="settings",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "unbound_dm"


def test_dm_bind_command_is_exempt():
    store = _Store()
    result = check_auth(
        user_id="U3",
        channel_id="D3",
        is_dm=True,
        action="bind",
        store=store,
    )

    assert result.allowed is True


def test_channel_auth_denies_unconfigured_channel():
    store = _Store()
    result = check_auth(
        user_id="U4",
        channel_id="C-missing",
        is_dm=False,
        action="",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "unauthorized_channel"


def test_admin_guard_denies_non_admin_for_protected_action():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True)
    store._bound_users.add("U5")
    store._admins.add("U-admin")

    result = check_auth(
        user_id="U5",
        channel_id="C1",
        is_dm=False,
        action="cmd_settings",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "not_admin"


def test_require_bind_channel_denies_unbound_user():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True, require_bind=True)

    result = check_auth(
        user_id="U-stranger",
        channel_id="C1",
        is_dm=False,
        action="",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "not_bound_channel"


def test_require_bind_channel_allows_bound_user():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True, require_bind=True)
    store._bound_users.add("U-me")

    result = check_auth(
        user_id="U-me",
        channel_id="C1",
        is_dm=False,
        action="",
        store=store,
    )

    assert result.allowed is True


def test_require_bind_channel_denies_disabled_bound_user():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True, require_bind=True)
    store._bound_users.add("U-disabled")
    store._disabled_users.add("U-disabled")

    result = check_auth(
        user_id="U-disabled",
        channel_id="C1",
        is_dm=False,
        action="",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "not_bound_channel"


def test_require_bind_off_allows_any_channel_member():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True, require_bind=None)

    result = check_auth(
        user_id="U-stranger",
        channel_id="C1",
        is_dm=False,
        action="",
        store=store,
    )

    assert result.allowed is True


def test_admin_guard_denies_non_admin_for_auth_setup_callback():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True)
    store._bound_users.add("U5")
    store._admins.add("U-admin")

    result = check_auth(
        user_id="U5",
        channel_id="C1",
        is_dm=False,
        action="auth_setup:codex",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "not_admin"


def test_admin_guard_stays_enabled_when_only_admin_is_disabled():
    store = _Store()
    store.settings.channels["C1"] = SimpleNamespace(enabled=True)
    store._admins.add("U-disabled-admin")
    store._disabled_users.add("U-disabled-admin")

    result = check_auth(
        user_id="U-anyone",
        channel_id="C1",
        is_dm=False,
        action="settings",
        store=store,
    )

    assert result.allowed is False
    assert result.denial == "not_admin"
