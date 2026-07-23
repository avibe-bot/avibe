from __future__ import annotations

import json
from pathlib import Path

from config.v2_settings import ChannelSettings, SettingsState
from core import chat_discovery
from storage.migrations import run_migrations
from storage.settings_service import SQLiteSettingsService


def _auth_context(platform: str, **kwargs) -> str:
    value = chat_discovery._auth_context_for(platform, kwargs)
    assert value is not None
    return value


def test_metadata_merge_preserves_unknown_keys_and_sticky_true_flags() -> None:
    merged = chat_discovery.merge_metadata(
        {
            "custom": "keep",
            chat_discovery.METADATA_IS_FORUM: True,
            chat_discovery.METADATA_TOPIC: "old",
        },
        {
            chat_discovery.METADATA_IS_FORUM: False,
            chat_discovery.METADATA_TOPIC: "new",
            chat_discovery.METADATA_VISIBILITY_STATUS: chat_discovery.VISIBILITY_VISIBLE,
        },
    )

    assert merged["custom"] == "keep"
    assert merged[chat_discovery.METADATA_IS_FORUM] is True
    assert merged[chat_discovery.METADATA_TOPIC] == "new"
    assert merged[chat_discovery.METADATA_VISIBILITY_STATUS] == chat_discovery.VISIBILITY_VISIBLE


def test_remember_chat_lists_inventory_with_configured_state(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)

    chat_discovery.remember_chat(
        "telegram",
        "123",
        name="General",
        native_type="supergroup",
        is_private=False,
        supports_threads=True,
        metadata={
            chat_discovery.METADATA_USERNAME: "general",
            chat_discovery.METADATA_IS_FORUM: True,
        },
        db_path=db_path,
    )

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(SettingsState(channels={"telegram::123": ChannelSettings(enabled=True)}))
    finally:
        service.close()

    chats = chat_discovery.list_chats("telegram", db_path=db_path)

    assert len(chats) == 1
    assert chats[0].chat_id == "123"
    assert chats[0].name == "General"
    assert chats[0].configured is True
    assert chats[0].metadata[chat_discovery.METADATA_USERNAME] == "general"
    assert chats[0].visibility_status == chat_discovery.VISIBILITY_VISIBLE


def test_remember_thread_lists_discovered_and_configured_topics(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat(
        "telegram",
        "-1001",
        name="Engineering",
        native_type="supergroup",
        supports_threads=True,
        db_path=db_path,
    )
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "42",
        name="Releases",
        native_type="forum_topic",
        db_path=db_path,
    )

    topics = chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path)
    assert [(topic["id"], topic["name"], topic["configured"]) for topic in topics] == [
        ("42", "Releases", False)
    ]

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                threads={
                    "telegram::-1001/42": ChannelSettings(enabled=True, require_mention=False),
                }
            )
        )
    finally:
        service.close()

    topics = chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path)
    assert topics[0]["configured"] is True
    assert topics[0]["name"] == "Releases"


def test_passively_discovered_topics_leave_fallback_names_to_ui(tmp_path: Path) -> None:
    # Scenario: TELEGRAM-TOPIC-004
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat(
        "telegram",
        "-1001",
        name="Engineering",
        native_type="supergroup",
        supports_threads=True,
        db_path=db_path,
    )
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "1",
        native_type="forum_topic",
        db_path=db_path,
    )
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "42",
        native_type="forum_topic",
        db_path=db_path,
    )

    topics = chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path)

    assert [(topic["id"], topic["name"]) for topic in topics] == [("1", ""), ("42", "")]


def test_remember_chat_debounce_does_not_suppress_retry_after_persist_failure(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    original_upsert_scope = chat_discovery.upsert_scope
    calls = 0

    def flaky_upsert_scope(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return original_upsert_scope(*args, **kwargs)

    monkeypatch.setattr(chat_discovery, "upsert_scope", flaky_upsert_scope)

    try:
        chat_discovery.remember_chat("telegram", "retry", name="Retry", db_path=db_path)
    except RuntimeError as exc:
        assert str(exc) == "database is locked"

    chat_discovery.remember_chat("telegram", "retry", name="Retry", db_path=db_path)

    chats = chat_discovery.list_chats("telegram", db_path=db_path)

    assert calls == 2
    assert [chat.chat_id for chat in chats] == ["retry"]


def test_legacy_discovered_chats_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    legacy_path = tmp_path / "discovered_chats.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "platforms": {
                    "telegram": {
                        "456": {
                            "name": "Ops",
                            "username": "ops",
                            "chat_type": "supergroup",
                            "is_private": False,
                            "is_forum": True,
                            "supports_topics": True,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    chat_discovery.migrate_legacy_discovered_chats(db_path=db_path, legacy_path=legacy_path)
    chat_discovery.migrate_legacy_discovered_chats(db_path=db_path, legacy_path=legacy_path)

    chats = chat_discovery.list_chats("telegram", db_path=db_path)

    assert [chat.chat_id for chat in chats] == ["456"]
    assert chats[0].supports_threads is True
    assert legacy_path.exists() is False
    assert legacy_path.with_suffix(".json.migrated").exists()
    assert chat_discovery.get_state_meta("migrations.discovered_chats_to_scopes", db_path=db_path) == "done"


def test_legacy_migration_rename_tolerates_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "discovered_chats.json"
    target = tmp_path / "discovered_chats.json.migrated"

    chat_discovery._rename_preserving_existing(source, target)

    assert source.exists() is False
    assert target.exists() is False


def test_refresh_marks_absent_rows_not_returned_without_deleting_settings(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat(
        "slack",
        "C_OLD",
        name="old",
        metadata={
            chat_discovery.METADATA_AUTH_CONTEXT: _auth_context("slack", bot_token="x"),
            chat_discovery.METADATA_IS_MEMBER: True,
        },
        db_path=db_path,
    )

    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(SettingsState(channels={"slack::C_OLD": ChannelSettings(enabled=True)}))
    finally:
        service.close()

    from vibe import api

    monkeypatch.setattr(
        api,
        "list_channels_live",
        lambda _token, browse_all=False: {
            "ok": True,
            "channels": [{"id": "C_NEW", "name": "new", "is_private": False}],
            "is_member_only": not browse_all,
        },
    )

    result = chat_discovery.refresh_platform("slack", force=True, bot_token="x", db_path=db_path)
    chats = {chat.chat_id: chat for chat in chat_discovery.list_chats("slack", db_path=db_path)}

    assert result.ok is True
    assert chats["C_NEW"].visibility_status == chat_discovery.VISIBILITY_VISIBLE
    assert chats["C_OLD"].visibility_status == chat_discovery.VISIBILITY_NOT_RETURNED
    assert chats["C_OLD"].configured is True


def test_refresh_failure_keeps_stale_cache_and_records_error(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat(
        "slack",
        "C_KEEP",
        name="keep",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: _auth_context("slack", bot_token="x")},
        db_path=db_path,
    )

    from vibe import api

    monkeypatch.setattr(api, "list_channels_live", lambda _token, browse_all=False: {"ok": False, "error": "boom"})

    result = chat_discovery.refresh_platform("slack", force=True, bot_token="x", db_path=db_path)
    chats = chat_discovery.list_chats("slack", db_path=db_path)
    state = chat_discovery.refresh_state("slack", refresh_scope=_auth_context("slack", bot_token="x"), db_path=db_path)
    response = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)

    assert result.ok is False
    assert result.refresh_state.last_error == "boom"
    assert chats[0].chat_id == "C_KEEP"
    assert state.last_error == "boom"
    assert response["ok"] is True
    assert response["channels"][0]["id"] == "C_KEEP"
    assert response["error"] == "boom"


def test_empty_cache_channel_response_respects_refresh_backoff(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    calls = 0

    from vibe import api

    def fail_refresh(_token: str, browse_all: bool = False) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": False, "error": "bad token"}

    monkeypatch.setattr(api, "list_channels_live", fail_refresh)

    first = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)
    second = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)

    assert calls == 1
    assert first["ok"] is False
    assert first["error"] == "bad token"
    assert second["ok"] is False
    assert second["error"] == "bad token"


def test_stale_cache_schedules_only_one_background_refresh(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_STALE",
        name="stale",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        db_path=db_path,
    )
    chat_discovery.set_state_meta(
        f"channel_refresh.slack.{auth_context}",
        {
            "last_attempt_at": "2000-01-01T00:00:00+00:00",
            "last_success_at": "2000-01-01T00:00:00+00:00",
            "last_error": None,
        },
        db_path=db_path,
    )
    starts = 0

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            nonlocal starts
            starts += 1

    monkeypatch.setattr(chat_discovery.threading, "Thread", FakeThread)

    try:
        first = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)
        second = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)
    finally:
        with chat_discovery._scheduled_refreshes_lock:
            chat_discovery._scheduled_refreshes.clear()

    assert starts == 1
    assert first["refreshing"] is True
    assert second["refreshing"] is True


def test_slack_cached_response_respects_member_only_browse_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_MEMBER",
        name="member",
        metadata={chat_discovery.METADATA_IS_MEMBER: True, chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        db_path=db_path,
    )
    chat_discovery.remember_chat(
        "slack",
        "C_OTHER",
        name="other",
        metadata={chat_discovery.METADATA_IS_MEMBER: False, chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        db_path=db_path,
    )
    chat_discovery.set_state_meta(
        f"channel_refresh.slack.{auth_context}",
        {"last_attempt_at": "2999-01-01T00:00:00+00:00", "last_success_at": "2999-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    member_only = chat_discovery.channels_response("slack", require_member=True, bot_token="x", db_path=db_path)
    browse_all = chat_discovery.channels_response("slack", require_member=False, bot_token="x", db_path=db_path)

    assert [channel["id"] for channel in member_only["channels"]] == ["C_MEMBER"]
    assert {channel["id"] for channel in browse_all["channels"]} == {"C_MEMBER", "C_OTHER"}


def test_slack_member_only_response_excludes_not_returned_member_channels(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_GONE",
        name="gone",
        metadata={
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_VISIBILITY_STATUS: chat_discovery.VISIBILITY_NOT_RETURNED,
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
        },
        db_path=db_path,
    )
    chat_discovery.remember_chat(
        "slack",
        "C_PRESENT",
        name="present",
        metadata={
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
        },
        db_path=db_path,
    )
    chat_discovery.set_state_meta(
        f"channel_refresh.slack.{auth_context}",
        {"last_attempt_at": "2999-01-01T00:00:00+00:00", "last_success_at": "2999-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    response = chat_discovery.channels_response("slack", require_member=True, bot_token="x", db_path=db_path)

    assert [channel["id"] for channel in response["channels"]] == ["C_PRESENT"]


def test_slack_member_only_opt_in_exposes_not_returned_member_channels(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_GONE_OPTIN",
        name="gone",
        metadata={
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_VISIBILITY_STATUS: chat_discovery.VISIBILITY_NOT_RETURNED,
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
        },
        db_path=db_path,
    )
    chat_discovery.remember_chat(
        "slack",
        "C_PRESENT_OPTIN",
        name="present",
        metadata={
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
        },
        db_path=db_path,
    )
    chat_discovery.set_state_meta(
        f"channel_refresh.slack.{auth_context}",
        {"last_attempt_at": "2999-01-01T00:00:00+00:00", "last_success_at": "2999-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    # Member-only view (Slack default) must still surface the stale member row
    # when the caller opts in, so it can be reviewed/removed without browse-all.
    response = chat_discovery.channels_response(
        "slack", require_member=True, include_not_returned=True, bot_token="x", db_path=db_path
    )

    assert {c["id"] for c in response["channels"]} == {"C_GONE_OPTIN", "C_PRESENT_OPTIN"}
    assert response["summary"]["not_returned_count"] == 1


def test_slack_browse_all_refreshes_after_member_only_cache_hit(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    calls: list[bool] = []

    from vibe import api

    def fake_list_channels_live(_token: str, browse_all: bool = False) -> dict:
        calls.append(browse_all)
        channels = [{"id": "C_MEMBER", "name": "member", "is_private": False, "is_member": True}]
        if browse_all:
            channels.append({"id": "C_OTHER", "name": "other", "is_private": False, "is_member": False})
        return {"ok": True, "channels": channels, "is_member_only": not browse_all}

    monkeypatch.setattr(api, "list_channels_live", fake_list_channels_live)

    member_only = chat_discovery.channels_response(
        "slack",
        bot_token="x",
        browse_all=False,
        require_member=True,
        db_path=db_path,
    )
    browse_all = chat_discovery.channels_response(
        "slack",
        bot_token="x",
        browse_all=True,
        require_member=False,
        db_path=db_path,
    )
    cached_browse_all = chat_discovery.channels_response(
        "slack",
        bot_token="x",
        browse_all=True,
        require_member=False,
        db_path=db_path,
    )

    assert calls == [False, True]
    assert [channel["id"] for channel in member_only["channels"]] == ["C_MEMBER"]
    assert {channel["id"] for channel in browse_all["channels"]} == {"C_MEMBER", "C_OTHER"}
    assert {channel["id"] for channel in cached_browse_all["channels"]} == {"C_MEMBER", "C_OTHER"}


def test_slack_channel_cache_is_scoped_by_auth_context(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    calls: list[str] = []

    from vibe import api

    def fake_list_channels_live(token: str, browse_all: bool = False) -> dict:
        calls.append(token)
        suffix = token[-1].upper()
        return {
            "ok": True,
            "channels": [{"id": f"C_{suffix}", "name": f"workspace-{suffix}", "is_private": False, "is_member": True}],
            "is_member_only": not browse_all,
        }

    monkeypatch.setattr(api, "list_channels_live", fake_list_channels_live)

    first = chat_discovery.channels_response("slack", bot_token="token-a", db_path=db_path)
    second = chat_discovery.channels_response("slack", bot_token="token-b", db_path=db_path)
    cached_first = chat_discovery.channels_response("slack", bot_token="token-a", db_path=db_path)

    assert calls == ["token-a", "token-b"]
    assert [channel["id"] for channel in first["channels"]] == ["C_A"]
    assert [channel["id"] for channel in second["channels"]] == ["C_B"]
    assert [channel["id"] for channel in cached_first["channels"]] == ["C_A"]


def test_active_channel_response_rejects_missing_auth_context_without_cache_read(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat(
        "slack",
        "C_OLD",
        name="old workspace",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: _auth_context("slack", bot_token="x")},
        db_path=db_path,
    )

    response = chat_discovery.channels_response("slack", bot_token="", db_path=db_path)

    assert response["ok"] is False
    assert response["channels"] == []
    assert response["error"] == "Missing slack channel refresh credentials"


def test_discord_refresh_state_is_scoped_by_guild(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    calls: list[str] = []

    from vibe import api

    def fake_discord_channels(_token: str, guild_id: str) -> dict:
        calls.append(guild_id)
        return {
            "ok": True,
            "channels": [{"id": f"C_{guild_id}", "name": f"channel-{guild_id}", "type": 0}],
        }

    monkeypatch.setattr(api, "discord_list_channels_live", fake_discord_channels)

    first = chat_discovery.refresh_platform("discord", bot_token="x", guild_id="G1", db_path=db_path)
    second = chat_discovery.refresh_platform("discord", bot_token="x", guild_id="G2", db_path=db_path)

    assert first.ok is True
    assert second.ok is True
    assert calls == ["G1", "G2"]
    assert (
        chat_discovery.refresh_state(
            "discord",
            refresh_scope=f"guild.G1.{_auth_context('discord', bot_token='x')}",
            db_path=db_path,
        ).last_success_at
        is not None
    )
    assert (
        chat_discovery.refresh_state(
            "discord",
            refresh_scope=f"guild.G2.{_auth_context('discord', bot_token='x')}",
            db_path=db_path,
        ).last_success_at
        is not None
    )
    assert chat_discovery.refresh_state("discord", db_path=db_path).last_success_at is None


def test_discord_cached_payload_preserves_numeric_channel_type(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)

    from vibe import api

    monkeypatch.setattr(
        api,
        "discord_list_channels_live",
        lambda _token, _guild_id: {
            "ok": True,
            "channels": [{"id": "C_TEXT", "name": "text", "type": 0}],
        },
    )

    response = chat_discovery.channels_response(
        "discord",
        bot_token="x",
        guild_id="G1",
        parent_scope_id="discord::guild::G1",
        db_path=db_path,
    )
    cached = chat_discovery.channels_response(
        "discord",
        bot_token="x",
        guild_id="G1",
        parent_scope_id="discord::guild::G1",
        db_path=db_path,
    )

    assert response["channels"][0]["type"] == 0
    assert cached["channels"][0]["type"] == 0
    assert cached["channels"][0]["native_type"] == "0"


def _seed_not_returned_discord(db_path: Path) -> None:
    auth_context = _auth_context("discord", bot_token="x")
    parent = chat_discovery.make_scope_id("discord", chat_discovery.GUILD_SCOPE_TYPE, "G1")
    # Parent guild scope must exist before channels can reference it (FK).
    chat_discovery._remember_guild("discord", "G1", db_path=db_path)
    chat_discovery.remember_chat(
        "discord",
        "C_PRESENT",
        name="present",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        parent_id=parent,
        db_path=db_path,
    )
    chat_discovery.remember_chat(
        "discord",
        "C_GONE",
        name="gone",
        metadata={
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
            chat_discovery.METADATA_VISIBILITY_STATUS: chat_discovery.VISIBILITY_NOT_RETURNED,
            chat_discovery.METADATA_LAST_MISSING_AT: "2026-01-01T00:00:00+00:00",
        },
        parent_id=parent,
        db_path=db_path,
    )


def test_channels_response_hides_not_returned_by_default_and_opts_in(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    _seed_not_returned_discord(db_path)
    # Mark already fetched so no live refresh is attempted.
    chat_discovery.set_state_meta(
        f"channel_refresh.discord.guild.G1.{_auth_context('discord', bot_token='x')}",
        {"last_attempt_at": "2999-01-01T00:00:00+00:00", "last_success_at": "2999-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    parent = chat_discovery.make_scope_id("discord", chat_discovery.GUILD_SCOPE_TYPE, "G1")
    hidden = chat_discovery.channels_response(
        "discord", bot_token="x", guild_id="G1", parent_scope_id=parent, db_path=db_path
    )
    shown = chat_discovery.channels_response(
        "discord",
        bot_token="x",
        guild_id="G1",
        parent_scope_id=parent,
        include_not_returned=True,
        db_path=db_path,
    )

    assert [c["id"] for c in hidden["channels"]] == ["C_PRESENT"]
    assert {c["id"] for c in shown["channels"]} == {"C_PRESENT", "C_GONE"}
    # Summary always counts the full inventory regardless of the view.
    assert hidden["summary"]["discovered_count"] == 2
    assert hidden["summary"]["not_returned_count"] == 1
    assert shown["summary"]["not_returned_count"] == 1


def test_all_not_returned_does_not_trigger_refresh_each_call(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_STALE_GUARD",
        name="gone",
        metadata={
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_VISIBILITY_STATUS: chat_discovery.VISIBILITY_NOT_RETURNED,
        },
        db_path=db_path,
    )
    # last_success recent (TTL not elapsed) but last_attempt old (backoff would
    # allow). Old `not chats` guard would refresh; the success-based guard must not.
    chat_discovery.set_state_meta(
        f"channel_refresh.slack.{auth_context}",
        {"last_attempt_at": "2000-01-01T00:00:00+00:00", "last_success_at": "2999-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    from vibe import api

    calls = 0

    def fake_live(_token: str, browse_all: bool = False) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": True, "channels": [], "is_member_only": not browse_all}

    monkeypatch.setattr(api, "list_channels_live", fake_live)

    first = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)
    second = chat_discovery.channels_response("slack", bot_token="x", db_path=db_path)

    assert calls == 0
    assert first["channels"] == []
    assert second["channels"] == []
    assert first["summary"]["not_returned_count"] == 1


def test_refresh_skips_not_returned_marking_when_lark_truncated(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("lark", app_id="a", app_secret="s", domain="feishu")
    chat_discovery.remember_chat(
        "lark",
        "oc_keep",
        name="keep",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        db_path=db_path,
    )

    from vibe import api

    # Live result is truncated and does NOT include oc_keep — it must NOT be
    # marked not_returned because the inventory is incomplete.
    monkeypatch.setattr(
        api,
        "lark_list_chats_live",
        lambda _app_id, _app_secret, _domain="feishu": {
            "ok": True,
            "channels": [{"id": "oc_new", "name": "new", "chat_type": "group"}],
            "truncated": True,
        },
    )

    result = chat_discovery.refresh_platform(
        "lark", force=True, app_id="a", app_secret="s", domain="feishu", db_path=db_path
    )
    chats = {c.chat_id: c for c in chat_discovery.list_chats("lark", db_path=db_path)}

    assert result.ok is True
    assert chats["oc_keep"].visibility_status != chat_discovery.VISIBILITY_NOT_RETURNED
    assert chats["oc_new"].visibility_status == chat_discovery.VISIBILITY_VISIBLE


def test_background_refresh_skips_marking_when_lark_truncated(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("lark", app_id="a", app_secret="s", domain="feishu")
    chat_discovery.remember_chat(
        "lark",
        "oc_keepbg",
        name="keep",
        metadata={chat_discovery.METADATA_AUTH_CONTEXT: auth_context},
        db_path=db_path,
    )
    # Stale cache so channels_response schedules a background refresh.
    chat_discovery.set_state_meta(
        f"channel_refresh.lark.{auth_context}",
        {"last_attempt_at": "2000-01-01T00:00:00+00:00", "last_success_at": "2000-01-01T00:00:00+00:00", "last_error": None},
        db_path=db_path,
    )

    from vibe import api

    monkeypatch.setattr(
        api,
        "lark_list_chats_live",
        lambda _app_id, _app_secret, _domain="feishu": {
            "ok": True,
            "channels": [{"id": "oc_newbg", "name": "new", "chat_type": "group"}],
            "truncated": True,
        },
    )

    class InlineThread:
        def __init__(self, *args, target=None, **kwargs):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    monkeypatch.setattr(chat_discovery.threading, "Thread", InlineThread)

    try:
        chat_discovery.channels_response(
            "lark", app_id="a", app_secret="s", domain="feishu", db_path=db_path
        )
    finally:
        with chat_discovery._scheduled_refreshes_lock:
            chat_discovery._scheduled_refreshes.clear()

    chats = {c.chat_id: c for c in chat_discovery.list_chats("lark", db_path=db_path)}
    assert chats["oc_keepbg"].visibility_status != chat_discovery.VISIBILITY_NOT_RETURNED


def test_delete_scope_removes_scope_and_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat("telegram", "999", name="Gone", db_path=db_path)
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(SettingsState(channels={"telegram::999": ChannelSettings(enabled=True)}))
    finally:
        service.close()

    assert any(c.chat_id == "999" for c in chat_discovery.list_chats("telegram", db_path=db_path))

    # No history → physical delete.
    outcome = chat_discovery.delete_scope("telegram", "999", db_path=db_path)
    assert outcome == {"removed": True, "dismissed": False}
    assert chat_discovery.list_chats("telegram", db_path=db_path) == []
    # Settings row is gone too (no orphan).
    reloaded = SQLiteSettingsService(db_path)
    try:
        assert "telegram::999" not in reloaded.load_state().channels
    finally:
        reloaded.close()
    # Deleting a missing scope is a no-op.
    assert chat_discovery.delete_scope("telegram", "999", db_path=db_path) == {
        "removed": False,
        "dismissed": False,
    }


def test_delete_scope_cascades_child_thread_settings(tmp_path: Path) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat("telegram", "-1001", name="Forum", db_path=db_path)
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "42",
        name="Releases",
        native_type="forum_topic",
        db_path=db_path,
    )
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                channels={"telegram::-1001": ChannelSettings(enabled=True)},
                threads={
                    "telegram::-1001/42": ChannelSettings(
                        enabled=True,
                        require_mention=False,
                    )
                },
            )
        )
    finally:
        service.close()

    outcome = chat_discovery.delete_scope("telegram", "-1001", db_path=db_path)

    assert outcome == {"removed": True, "dismissed": False}
    assert chat_discovery.list_chats("telegram", db_path=db_path) == []
    assert chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path) == []
    reloaded = SQLiteSettingsService(db_path)
    try:
        state = reloaded.load_state()
        assert "telegram::-1001" not in state.channels
        assert "telegram::-1001/42" not in state.threads
    finally:
        reloaded.close()


def test_delete_scope_preserves_child_thread_history(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from storage.models import messages

    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat("telegram", "-1001", name="Forum", db_path=db_path)
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "42",
        name="Releases",
        native_type="forum_topic",
        db_path=db_path,
    )
    service = SQLiteSettingsService(db_path)
    try:
        service.save_state(
            SettingsState(
                threads={
                    "telegram::-1001/42": ChannelSettings(enabled=True),
                }
            )
        )
    finally:
        service.close()

    thread_scope_id = chat_discovery.make_scope_id(
        "telegram",
        chat_discovery.THREAD_SCOPE_TYPE,
        "-1001/42",
    )
    parent_scope_id = chat_discovery.make_scope_id(
        "telegram",
        chat_discovery.CHANNEL_SCOPE_TYPE,
        "-1001",
    )
    now = datetime.now(timezone.utc).isoformat()
    engine = chat_discovery._engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                messages.insert(),
                [
                    {
                        "id": "parent-message",
                        "scope_id": parent_scope_id,
                        "platform": "telegram",
                        "author": "user",
                        "type": "user",
                        "content_json": "{}",
                        "metadata_json": "{}",
                        "created_at": now,
                        "updated_at": now,
                    },
                    {
                        "id": "topic-message",
                        "scope_id": thread_scope_id,
                        "platform": "telegram",
                        "author": "user",
                        "type": "user",
                        "content_json": "{}",
                        "metadata_json": "{}",
                        "created_at": now,
                        "updated_at": now,
                    },
                ],
            )
    finally:
        engine.dispose()

    outcome = chat_discovery.delete_scope("telegram", "-1001", db_path=db_path)

    assert outcome == {"removed": False, "dismissed": True}
    assert chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path) == []
    chat_discovery.remember_chat("telegram", "-1001", name="Forum", db_path=db_path)
    assert chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path) == []
    chat_discovery.remember_thread(
        "telegram",
        "-1001",
        "42",
        name="Releases",
        native_type="forum_topic",
        db_path=db_path,
    )
    assert [
        topic["id"]
        for topic in chat_discovery.list_thread_payloads("telegram", "-1001", db_path=db_path)
    ] == ["42"]
    engine = chat_discovery._engine(db_path)
    try:
        with engine.connect() as conn:
            kept = conn.execute(messages.select().where(messages.c.id == "topic-message")).first()
    finally:
        engine.dispose()
    assert kept is not None
    reloaded = SQLiteSettingsService(db_path)
    try:
        assert "telegram::-1001/42" not in reloaded.load_state().threads
    finally:
        reloaded.close()


def test_delete_scope_with_history_dismisses_instead_of_deleting(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from storage.models import messages

    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat("telegram", "777", name="HasHistory", db_path=db_path)
    scope_id = chat_discovery.make_scope_id("telegram", chat_discovery.CHANNEL_SCOPE_TYPE, "777")

    # Insert a message owned by the scope (CASCADE FK). A physical delete would
    # destroy it, so removal must dismiss instead.
    now = datetime.now(timezone.utc).isoformat()
    engine = chat_discovery._engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                messages.insert().values(
                    id="m1",
                    scope_id=scope_id,
                    platform="telegram",
                    author="user",
                    type="user",
                    content_json="{}",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
    finally:
        engine.dispose()

    outcome = chat_discovery.delete_scope("telegram", "777", db_path=db_path)
    assert outcome == {"removed": False, "dismissed": True}

    # Hidden from every listing, including the unavailable view.
    assert chat_discovery.list_chats("telegram", db_path=db_path) == []
    assert chat_discovery.list_chats("telegram", include_not_returned=True, db_path=db_path) == []

    # History preserved (the scope row was kept, not cascade-deleted).
    engine = chat_discovery._engine(db_path)
    try:
        with engine.connect() as conn:
            kept = conn.execute(messages.select().where(messages.c.scope_id == scope_id)).first()
    finally:
        engine.dispose()
    assert kept is not None


def test_remember_chat_clears_dismissed_on_passive_rediscovery(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from storage.models import messages

    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    chat_discovery.remember_chat("telegram", "tg_redisc", name="Gone", db_path=db_path)
    scope_id = chat_discovery.make_scope_id("telegram", chat_discovery.CHANNEL_SCOPE_TYPE, "tg_redisc")

    now = datetime.now(timezone.utc).isoformat()
    engine = chat_discovery._engine(db_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                messages.insert().values(
                    id="m_redisc",
                    scope_id=scope_id,
                    platform="telegram",
                    author="user",
                    type="user",
                    content_json="{}",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
    finally:
        engine.dispose()

    # History present → removal dismisses (keeps row hidden).
    assert chat_discovery.delete_scope("telegram", "tg_redisc", db_path=db_path) == {
        "removed": False,
        "dismissed": True,
    }
    assert chat_discovery.list_chats("telegram", db_path=db_path) == []

    # Telegram has no live list; a new message flows through remember_chat, which
    # must clear the dismissal so the group reappears in Group Settings.
    chat_discovery.remember_chat("telegram", "tg_redisc", name="Active Again", db_path=db_path)
    assert [c.chat_id for c in chat_discovery.list_chats("telegram", db_path=db_path)] == ["tg_redisc"]


def test_refresh_clears_dismissed_flag_on_rediscovery(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    auth_context = _auth_context("slack", bot_token="x")
    chat_discovery.remember_chat(
        "slack",
        "C_BACK",
        name="back",
        metadata={
            chat_discovery.METADATA_AUTH_CONTEXT: auth_context,
            chat_discovery.METADATA_IS_MEMBER: True,
            chat_discovery.METADATA_DISMISSED_AT: "2026-01-01T00:00:00+00:00",
        },
        db_path=db_path,
    )
    # Dismissed → hidden everywhere initially.
    assert chat_discovery.list_chats("slack", include_not_returned=True, db_path=db_path) == []

    from vibe import api

    monkeypatch.setattr(
        api,
        "list_channels_live",
        lambda _token, browse_all=False: {
            "ok": True,
            "channels": [{"id": "C_BACK", "name": "back", "is_private": False, "is_member": True}],
            "is_member_only": not browse_all,
        },
    )

    chat_discovery.refresh_platform("slack", force=True, bot_token="x", db_path=db_path)

    chats = chat_discovery.list_chats("slack", db_path=db_path)
    assert [c.chat_id for c in chats] == ["C_BACK"]
    assert chats[0].visibility_status == chat_discovery.VISIBILITY_VISIBLE


def test_malformed_legacy_discovered_chats_does_not_break_channel_response(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "vibe.sqlite"
    run_migrations(db_path)
    legacy_path = tmp_path / "discovered_chats.json"
    legacy_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(chat_discovery.paths, "get_discovered_chats_path", lambda: legacy_path)

    from vibe import api

    monkeypatch.setattr(
        api,
        "list_channels_live",
        lambda _token, browse_all=False: {
            "ok": True,
            "channels": [{"id": "C1", "name": "general", "is_private": False, "is_member": True}],
            "is_member_only": not browse_all,
        },
    )

    response = chat_discovery.channels_response("slack", bot_token="x", require_member=True, db_path=db_path)

    assert response["ok"] is True
    assert response["channels"][0]["id"] == "C1"
    assert legacy_path.exists()
    assert chat_discovery.get_state_meta("migrations.discovered_chats_to_scopes", db_path=db_path) is None
