from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_settings import SettingsStore, UserSettings
from config.v2_config import UpdateConfig
from core import update_checker
from core.update_checker import UpdateChecker


RELEASE_URL_101 = "https://github.com/avibe-bot/avibe/releases/tag/v1.0.1"


class _StubSettingsManager:
    def __init__(self, store):
        self._store = store

    def get_store(self):
        return self._store


class _StubController:
    def __init__(self, store):
        self.settings_manager = _StubSettingsManager(store)
        self.config = type("Config", (), {"platform": "slack"})()
        self.im_client = object()
        self.im_clients = {}


class _FakeIMClient:
    def __init__(self, message_id="msg-1"):
        self.message_id = message_id
        self.dm_calls = []
        self.edit_calls = []

    async def send_dm(self, user_id: str, text: str, **kwargs):
        self.dm_calls.append((user_id, text, kwargs))
        return self.message_id

    async def edit_message(self, context, message_id: str, text: str, **kwargs):
        self.edit_calls.append((context, message_id, text, kwargs))
        return True


def test_get_admin_user_ids_includes_all_platforms(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("slack", {"U1": UserSettings(display_name="Slack", is_admin=True)})
    store.set_users_for_platform("discord", {"D1": UserSettings(display_name="Discord", is_admin=True)})
    store.save()

    checker = UpdateChecker(_StubController(store), UpdateConfig())

    admin_ids = checker._get_admin_user_ids()

    assert set(admin_ids) == {"slack::U1", "discord::D1"}


def test_update_notification_admin_dms_include_buttons_except_wechat(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("slack", {"U1": UserSettings(display_name="Slack", is_admin=True)})
    store.set_users_for_platform("discord", {"123456789012345678": UserSettings(display_name="Discord", is_admin=True)})
    store.set_users_for_platform("telegram", {"123456": UserSettings(display_name="Telegram", is_admin=True)})
    store.set_users_for_platform("lark", {"ou_admin": UserSettings(display_name="Lark", is_admin=True)})
    store.set_users_for_platform("wechat", {"wx_admin": UserSettings(display_name="WeChat", is_admin=True)})
    store.save()

    controller = _StubController(store)
    clients = {platform: _FakeIMClient() for platform in ["slack", "discord", "telegram", "lark", "wechat"]}
    controller.im_clients = clients
    controller.im_client = clients["slack"]
    checker = UpdateChecker(controller, UpdateConfig())

    delivered = asyncio.run(checker._send_update_notification("1.0.0", "1.0.1"))

    assert delivered is True
    slack_kwargs = clients["slack"].dm_calls[0][2]
    assert slack_kwargs["blocks"][1]["elements"][0]["action_id"] == "vibe_update_now"
    assert slack_kwargs["blocks"][1]["elements"][0]["value"] == "1.0.1"
    assert f"<{RELEASE_URL_101}|1.0.1>" in slack_kwargs["blocks"][0]["text"]["text"]
    assert RELEASE_URL_101 in clients["slack"].dm_calls[0][1]

    for platform in ["discord", "telegram", "lark"]:
        text = clients[platform].dm_calls[0][1]
        assert f"[1.0.1]({RELEASE_URL_101})" in text
        kwargs = clients[platform].dm_calls[0][2]
        keyboard = kwargs["keyboard"]
        assert keyboard.buttons[0][0].text == "Update Now"
        assert keyboard.buttons[0][0].callback_data == "vibe_update_now:1.0.1"

    assert f"1.0.1 ({RELEASE_URL_101})" in clients["wechat"].dm_calls[0][1]
    assert "keyboard" not in clients["wechat"].dm_calls[0][2]


def test_update_notification_release_url_normalizes_github_tags():
    assert update_checker._github_release_url("1.0.1") == RELEASE_URL_101
    assert update_checker._github_release_url("v1.0.1") == RELEASE_URL_101
    assert (
        update_checker._github_release_url("gh-v2.2.8rc1")
        == "https://github.com/avibe-bot/avibe/releases/tag/gh-v2.2.8rc1"
    )


def test_update_notification_policy_marker_parses_hidden_release_metadata():
    body = """
    ## Changes

    - Small internal fix.

    <!-- vibe-remote:update-notification=none -->
    """

    assert update_checker._parse_update_notification_policy(body) == "none"
    assert update_checker._parse_update_notification_policy("<!-- avibe:update-notification=none -->") == "none"
    assert update_checker._parse_update_notification_policy("## Changes") == "default"
    assert update_checker._parse_update_notification_policy(None) == "default"


def test_fetch_update_notification_policy_reads_github_release_body():
    payload = b'{"body": "<!-- vibe-remote:update-notification=none -->"}'

    with patch.object(update_checker.urllib.request, "urlopen", return_value=_FakeResponse(payload)) as urlopen:
        info = update_checker._fetch_update_notification_policy_sync("1.0.1")

    req = urlopen.call_args.args[0]
    assert req.full_url == "https://api.github.com/repos/avibe-bot/avibe/releases/tags/v1.0.1"
    assert req.headers["User-agent"] == "avibe-os"
    assert info == {"version": "1.0.1", "policy": "none", "error": None}


def test_update_notification_returns_false_when_all_admin_dms_fail(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("discord", {"123456789012345678": UserSettings(display_name="Discord", is_admin=True)})
    store.set_users_for_platform("telegram", {"123456": UserSettings(display_name="Telegram", is_admin=True)})
    store.save()

    controller = _StubController(store)
    controller.im_clients = {
        "discord": _FakeIMClient(message_id=None),
        "telegram": _FakeIMClient(message_id=None),
    }
    checker = UpdateChecker(controller, UpdateConfig())

    delivered = asyncio.run(checker._send_update_notification("1.0.0", "1.0.1"))

    assert delivered is False


def test_failed_update_notification_does_not_defer_idle_auto_update(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("discord", {"123456789012345678": UserSettings(display_name="Discord", is_admin=True)})
    store.save()

    controller = _StubController(store)
    controller.im_clients = {"discord": _FakeIMClient(message_id=None)}
    checker = UpdateChecker(controller, UpdateConfig(check_interval_minutes=1, notify_admins=True, auto_update=True))
    checker.state.last_activity_at = time.time() - 3600
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "1.0.0", "latest": "1.0.1", "has_update": True, "error": None},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_update_notification_policy_sync",
        lambda version: {"version": version, "policy": "default", "error": None},
    )
    monkeypatch.setattr(checker, "_is_idle", lambda: True)
    monkeypatch.setattr("vibe.runtime.get_service_main_path", lambda: Path("/pkg/service_main.py"))
    monkeypatch.setattr(update_checker, "get_running_vibe_path", lambda: "/tmp/vibe")
    performed = []

    async def fake_perform_update(target_version, **kwargs):
        performed.append((target_version, kwargs))
        return {"ok": True, "restarting": False, "message": "ok"}

    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())

    assert checker.state.notified_version is None
    assert checker.state.notified_at is None
    assert performed == [("1.0.1", {})]


def test_silent_release_metadata_skips_notifications_but_keeps_auto_update(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("slack", {"U1": UserSettings(display_name="Slack", is_admin=True)})
    store.save()

    controller = _StubController(store)
    controller.im_clients = {"slack": _FakeIMClient()}
    controller.im_client = controller.im_clients["slack"]
    checker = UpdateChecker(controller, UpdateConfig(check_interval_minutes=1, notify_admins=True, auto_update=True))
    checker.state.last_activity_at = time.time() - 3600
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "1.0.0", "latest": "1.0.1", "has_update": True, "error": None},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_update_notification_policy_sync",
        lambda version: {"version": version, "policy": "none", "error": None},
    )
    monkeypatch.setattr(checker, "_is_idle", lambda: True)
    monkeypatch.setattr("vibe.runtime.get_service_main_path", lambda: Path("/pkg/service_main.py"))
    monkeypatch.setattr(update_checker, "get_running_vibe_path", lambda: "/tmp/vibe")
    notified = []
    performed = []

    async def fake_send_update_notification(current, latest):
        notified.append((current, latest))
        return True

    async def fake_perform_update(target_version, **kwargs):
        performed.append((target_version, kwargs))
        return {"ok": True, "restarting": False, "message": "ok"}

    monkeypatch.setattr(checker, "_send_update_notification", fake_send_update_notification)
    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())

    assert notified == []
    assert checker.state.notified_version is None
    assert checker.state.notified_at is None
    assert performed == [("1.0.1", {"suppress_post_update_notification": True})]


def test_update_check_reconciles_askill_even_when_product_auto_update_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(
        _StubController(SettingsStore.get_instance()),
        UpdateConfig(check_interval_minutes=1, notify_admins=False, auto_update=False),
    )
    reconciled = []
    monkeypatch.setattr(
        "vibe.api.reconcile_askill_auto_update",
        lambda: reconciled.append(True) or {"ok": True, "action": "update"},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "1.0.0", "latest": "1.0.1", "has_update": True, "error": None},
    )
    performed = []

    async def fake_perform_update(target_version, **kwargs):
        performed.append((target_version, kwargs))
        return {"ok": True, "restarting": False, "message": "ok"}

    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())

    assert reconciled == [True]
    assert performed == []


def test_update_check_reconciles_askill_even_when_product_checks_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(_StubController(SettingsStore.get_instance()), UpdateConfig(check_interval_minutes=0))
    reconciled = []
    monkeypatch.setattr(
        "vibe.api.reconcile_askill_auto_update",
        lambda: reconciled.append(True) or {"ok": True, "skipped": True, "reason": "up_to_date"},
    )
    monkeypatch.setattr(update_checker, "_fetch_pypi_version_sync", lambda: pytest.fail("product check should be skipped"))

    asyncio.run(checker._do_check())

    assert reconciled == [True]


def test_suppressed_post_update_notification_writes_verification_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(_StubController(SettingsStore.get_instance()), UpdateConfig())

    monkeypatch.setattr(
        "vibe.api.do_upgrade",
        lambda restart: {"ok": True, "restarting": True, "message": "ok", "output": None},
    )

    result = asyncio.run(checker._perform_update("1.0.1", suppress_post_update_notification=True))

    assert result["ok"] is True
    marker = tmp_path / "state" / "pending_update_notification.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["version"] == "1.0.1"
    assert data["suppress_success_notification"] is True


def test_update_marker_records_platform_for_non_slack_callbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(_StubController(SettingsStore.get_instance()), UpdateConfig())

    checker._write_update_marker("1.0.1", channel_id="123456", message_id="42", platform="telegram")

    marker = tmp_path / "state" / "pending_update_notification.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["platform"] == "telegram"
    assert data["channel_id"] == "123456"
    assert data["message_id"] == "42"


def test_post_update_notification_uses_unicode_emoji_for_non_slack(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    controller = _StubController(store)
    telegram_client = _FakeIMClient()
    controller.im_clients = {"telegram": telegram_client}
    checker = UpdateChecker(controller, UpdateConfig())
    checker._write_update_marker("1.0.1", channel_id="123456", message_id="42", platform="telegram")
    monkeypatch.setattr("vibe.__version__", "1.0.1", raising=False)

    asyncio.run(checker.check_and_send_post_update_notification())

    assert telegram_client.edit_calls
    _, _, text, _ = telegram_client.edit_calls[0]
    assert text == "✅ Avibe has been updated to `1.0.1`"
    assert ":white_check_mark:" not in text


def test_suppressed_post_update_marker_verifies_without_success_message(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    controller = _StubController(SettingsStore.get_instance())
    telegram_client = _FakeIMClient()
    controller.im_clients = {"telegram": telegram_client}
    checker = UpdateChecker(controller, UpdateConfig())
    checker._write_update_marker(
        "1.0.1",
        channel_id="123456",
        message_id="42",
        platform="telegram",
        suppress_success_notification=True,
    )
    monkeypatch.setattr("vibe.__version__", "1.0.1", raising=False)

    asyncio.run(checker.check_and_send_post_update_notification())

    assert telegram_client.edit_calls == []
    marker = tmp_path / "state" / "pending_update_notification.json"
    assert not marker.exists()


def test_auto_update_skips_unattended_source_checkout_install(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(
        _StubController(SettingsStore.get_instance()),
        UpdateConfig(check_interval_minutes=1, notify_admins=False, auto_update=True),
    )
    checker.state.last_activity_at = time.time() - 3600
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "3.0.4.dev10+g4d621ef0a", "latest": "3.0.4", "has_update": True, "error": None},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_update_notification_policy_sync",
        lambda version: {"version": version, "policy": "default", "error": None},
    )
    monkeypatch.setattr(checker, "_is_idle", lambda: True)
    monkeypatch.setattr(update_checker, "get_running_vibe_path", lambda: "/tmp/dev-vibe")
    monkeypatch.setattr("vibe.runtime.get_service_main_path", lambda: Path("/repo/main.py"))
    performed = []

    async def fake_perform_update(target_version, **kwargs):
        performed.append((target_version, kwargs))
        return {"ok": True, "restarting": True, "message": "ok"}

    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())

    assert performed == []
    assert checker.state.blocked_auto_update_version is None


def test_restartless_auto_update_blocks_same_version_retry_and_notifies(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    store = SettingsStore.get_instance()
    store.set_users_for_platform("telegram", {"123456": UserSettings(display_name="Telegram", is_admin=True)})
    store.save()
    controller = _StubController(store)
    telegram_client = _FakeIMClient()
    controller.im_clients = {"telegram": telegram_client}
    checker = UpdateChecker(
        controller,
        UpdateConfig(check_interval_minutes=1, notify_admins=False, auto_update=True),
    )
    checker.state.last_activity_at = time.time() - 3600
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "3.0.3", "latest": "3.0.4", "has_update": True, "error": None},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_update_notification_policy_sync",
        lambda version: {"version": version, "policy": "default", "error": None},
    )
    monkeypatch.setattr(checker, "_is_idle", lambda: True)
    monkeypatch.setattr("vibe.runtime.get_service_main_path", lambda: Path("/pkg/service_main.py"))
    monkeypatch.setattr(update_checker, "get_running_vibe_path", lambda: "/tmp/vibe")
    attempts = []

    async def fake_perform_update(target_version, **kwargs):
        attempts.append((target_version, kwargs))
        return {"ok": True, "restarting": False, "message": "restart missing"}

    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())
    asyncio.run(checker._do_check())

    assert attempts == [("3.0.4", {})]
    assert telegram_client.dm_calls
    _, text, _ = telegram_client.dm_calls[0]
    assert "did not take effect" in text
    assert "`3.0.4`" in text
    assert "`3.0.3`" in text
    assert checker.state.blocked_auto_update_version == "3.0.4"
    assert checker.state.blocked_auto_update_reason == "restart_not_scheduled"


def test_install_failure_auto_update_remains_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(
        _StubController(SettingsStore.get_instance()),
        UpdateConfig(check_interval_minutes=1, notify_admins=False, auto_update=True),
    )
    checker.state.last_activity_at = time.time() - 3600
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "3.0.3", "latest": "3.0.4", "has_update": True, "error": None},
    )
    monkeypatch.setattr(
        update_checker,
        "_fetch_update_notification_policy_sync",
        lambda version: {"version": version, "policy": "default", "error": None},
    )
    monkeypatch.setattr(checker, "_is_idle", lambda: True)
    monkeypatch.setattr("vibe.runtime.get_service_main_path", lambda: Path("/pkg/service_main.py"))
    monkeypatch.setattr(update_checker, "get_running_vibe_path", lambda: "/tmp/vibe")
    attempts = []

    async def fake_perform_update(target_version, **kwargs):
        attempts.append((target_version, kwargs))
        return {"ok": False, "restarting": False, "message": "network down"}

    monkeypatch.setattr(checker, "_perform_update", fake_perform_update)

    asyncio.run(checker._do_check())
    asyncio.run(checker._do_check())

    assert attempts == [("3.0.4", {}), ("3.0.4", {})]
    assert checker.state.blocked_auto_update_version is None
    assert checker.state.blocked_auto_update_reason is None


def test_update_check_error_preserves_blocked_auto_update(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    checker = UpdateChecker(
        _StubController(SettingsStore.get_instance()),
        UpdateConfig(check_interval_minutes=1, notify_admins=False, auto_update=True),
    )
    checker._block_auto_update("3.0.4", "post_update_version_mismatch", current_version="3.0.3")
    monkeypatch.setattr(
        update_checker,
        "_fetch_pypi_version_sync",
        lambda: {"current": "3.0.3", "latest": None, "has_update": False, "error": "network down"},
    )

    asyncio.run(checker._do_check())

    assert checker.state.blocked_auto_update_version == "3.0.4"
    assert checker.state.blocked_auto_update_reason == "post_update_version_mismatch"
    assert checker.state.blocked_auto_update_current_version == "3.0.3"


def test_post_update_notification_accepts_newer_running_version(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    controller = _StubController(SettingsStore.get_instance())
    telegram_client = _FakeIMClient()
    controller.im_clients = {"telegram": telegram_client}
    checker = UpdateChecker(controller, UpdateConfig())
    checker._write_update_marker("3.0.4", channel_id="123456", message_id="42", platform="telegram")
    monkeypatch.setattr("vibe.__version__", "3.0.5", raising=False)

    asyncio.run(checker.check_and_send_post_update_notification())

    assert telegram_client.edit_calls
    _, _, text, _ = telegram_client.edit_calls[0]
    assert text == "✅ Avibe has been updated to `3.0.4`"
    assert checker.state.blocked_auto_update_version is None
    marker = tmp_path / "state" / "pending_update_notification.json"
    assert not marker.exists()


def test_post_update_notification_skips_success_when_running_version_mismatches(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()
    controller = _StubController(SettingsStore.get_instance())
    telegram_client = _FakeIMClient()
    controller.im_clients = {"telegram": telegram_client}
    checker = UpdateChecker(controller, UpdateConfig())
    checker._write_update_marker("3.0.4", channel_id="123456", message_id="42", platform="telegram")
    monkeypatch.setattr("vibe.__version__", "3.0.3", raising=False)

    asyncio.run(checker.check_and_send_post_update_notification())

    assert telegram_client.edit_calls
    _, _, text, _ = telegram_client.edit_calls[0]
    assert "did not take effect" in text
    assert "`3.0.4`" in text
    assert "`3.0.3`" in text
    assert checker.state.blocked_auto_update_version == "3.0.4"
    assert checker.state.blocked_auto_update_reason == "post_update_version_mismatch"
    assert checker.state.blocked_auto_update_current_version == "3.0.3"
    marker = tmp_path / "state" / "pending_update_notification.json"
    assert not marker.exists()


def test_stop_returns_cancellable_task(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    async def run_test():
        checker = UpdateChecker(_StubController(SettingsStore.get_instance()), UpdateConfig(check_interval_minutes=1))
        checker.start()
        await asyncio.sleep(0)
        task = checker.stop()
        assert task is not None
        await checker.wait_stopped(task)
        assert task.done()

    asyncio.run(run_test())


def test_start_keeps_running_for_managed_dependencies_when_product_checks_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    SettingsStore.reset_instance()

    async def run_test():
        checker = UpdateChecker(_StubController(SettingsStore.get_instance()), UpdateConfig(check_interval_minutes=0))
        checker.start()
        await asyncio.sleep(0)
        task = checker.stop()
        assert task is not None
        await checker.wait_stopped(task)

    asyncio.run(run_test())


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_fetch_pypi_version_sync_ignores_prerelease_for_stable_current(monkeypatch):
    payload = b"""
    {
      "info": {"version": "2.2.8rc1"},
      "releases": {
        "2.2.7": [{}],
        "2.2.8rc1": [{}]
      }
    }
    """

    with patch.object(update_checker.urllib.request, "urlopen", return_value=_FakeResponse(payload)) as urlopen:
        monkeypatch.setattr("vibe.__version__", "2.2.7", raising=False)
        info = update_checker._fetch_pypi_version_sync()

    req = urlopen.call_args.args[0]
    assert req.full_url == "https://pypi.org/pypi/avibe-os/json"
    assert req.headers["User-agent"] == "avibe-os"
    assert info == {"current": "2.2.7", "latest": "2.2.7", "has_update": False, "error": None}
