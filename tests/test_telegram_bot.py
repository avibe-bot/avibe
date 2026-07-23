from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import paths
from core import chat_discovery
from core.message_context import resolve_context_thread_id
from modules.agents.native_sessions import NativeResumeSession
from modules.im import InlineButton, InlineKeyboard, MessageContext
from modules.im.multi import MultiIMClient
from modules.im.telegram import TelegramBot
from modules.im import telegram_api
from config.v2_config import TelegramConfig


@pytest.fixture(autouse=True)
def _reset_chat_discovery_cache():
    """chat_discovery keeps process-global debounce/migration caches keyed on
    (platform, chat_id) — not on the DB path — so a chat remembered by one test
    short-circuits remember_chat in a later test that points get_vibe_remote_dir at
    a fresh tmp dir. Clear them between tests for isolation."""
    chat_discovery._debounce_cache.clear()
    chat_discovery._migrated_db_paths.clear()
    yield


def test_normalize_command_text_strips_bot_mention() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}

    assert bot._normalize_command_text("/start@vibe_remote_bot hello") == "/start hello"


def test_strip_leading_bot_mention_returns_empty_for_mention_only() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    message = {
        "text": "@vibe_remote_bot",
        "entities": [{"type": "mention", "offset": 0, "length": 16}],
    }

    assert bot._strip_leading_bot_mention(message, "@vibe_remote_bot") == ""


def test_discovered_general_topic_leaves_name_for_ui_localization() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    chat = {
        "id": -100123,
        "type": "supergroup",
        "title": "Forum",
        "is_forum": True,
    }
    message = {"is_topic_message": True}

    with (
        patch.object(chat_discovery, "remember_chat"),
        patch.object(chat_discovery, "remember_thread") as remember_thread,
    ):
        bot._remember_discovered_chat(chat, message)

    remember_thread.assert_called_once_with(
        "telegram",
        "-100123",
        "1",
        name="",
        native_type="forum_topic",
        metadata={"is_general": True},
    )


def test_strip_leading_bot_mention_keeps_message_body() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    message = {
        "text": "@vibe_remote_bot hello there",
        "entities": [{"type": "mention", "offset": 0, "length": 16}],
    }

    assert bot._strip_leading_bot_mention(message, "@vibe_remote_bot hello there") == "hello there"


def test_group_message_uses_channel_require_mention_override() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", require_mention=True))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    bot.settings_manager = SimpleNamespace(get_require_mention=lambda channel_id, global_default=False: False)
    bot.on_message_callback = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 77,
                "chat": {"id": -100123, "type": "group", "title": "Core Group"},
                "from": {"id": 42},
                "text": "hello team",
            }
        )
    )

    bot.on_message_callback.assert_awaited_once()
    assert bot.on_message_callback.await_args.args[1] == "hello team"


def test_forum_message_uses_topic_require_mention_override() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", require_mention=True))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    settings_keys: list[str] = []

    def get_require_mention(settings_key, global_default=False):
        settings_keys.append(settings_key)
        return False

    bot.settings_manager = SimpleNamespace(get_require_mention=get_require_mention)
    bot.on_message_callback = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 78,
                "message_thread_id": 42,
                "is_topic_message": True,
                "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum", "is_forum": True},
                "from": {"id": 42},
                "text": "hello topic",
            }
        )
    )

    assert settings_keys == ["thread::-100123::42"]
    bot.on_message_callback.assert_awaited_once()


def test_inbound_message_refreshes_config_before_reading_options() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", require_mention=True))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    refresh_calls: list[bool] = []

    def _refresh_config_from_disk() -> None:
        refresh_calls.append(True)
        bot.config.require_mention = False

    bot.set_controller(SimpleNamespace(_refresh_config_from_disk=_refresh_config_from_disk))
    bot.on_message_callback = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 77,
                "chat": {"id": -100123, "type": "group", "title": "Core Group"},
                "from": {"id": 42},
                "text": "hello team",
            }
        )
    )

    assert refresh_calls == [True]
    bot.on_message_callback.assert_awaited_once()


def test_group_mention_only_falls_through_as_empty_message() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", require_mention=True))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    bot.on_message_callback = AsyncMock()
    message = {
        "message_id": 77,
        "chat": {"id": -100123, "type": "group", "title": "Core Group"},
        "from": {"id": 42},
        "text": "@vibe_remote_bot",
        "entities": [{"type": "mention", "offset": 0, "length": 16}],
    }

    asyncio.run(bot._handle_message(message))

    bot.on_message_callback.assert_awaited_once()
    assert bot.on_message_callback.await_args.args[1] == ""


def test_extract_files_includes_voice_and_audio_messages() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    files = bot._extract_files(
        {
            "voice": {"file_id": "voice-file", "mime_type": "audio/ogg", "file_size": 1234},
            "audio": {
                "file_id": "audio-file",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "file_size": 5678,
            },
        }
    )

    assert [(file.name, file.mimetype, file.url, file.size) for file in files] == [
        ("telegram-voice.ogg", "audio/ogg", "voice-file", 1234),
        ("song.mp3", "audio/mpeg", "audio-file", 5678),
    ]


def test_plain_group_sessions_are_channel_scoped() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    assert bot.should_use_message_id_for_channel_session() is False


def test_forum_general_message_auto_creates_topic() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        message_id="77",
        platform="telegram",
        platform_specific={"chat_type": "supergroup", "is_topic_message": True},
    )
    message = {
        "from": {"first_name": "Alex"},
        "is_topic_message": True,
        "message_thread_id": 1,
    }

    with patch(
        "modules.im.telegram.telegram_api.create_forum_topic",
        new=AsyncMock(return_value={"result": {"message_thread_id": 88}}),
    ):
        with patch.object(bot, "send_message", new=AsyncMock(return_value="1")) as send_mock:
            topic_context = asyncio.run(bot.start_new_topic_session(context, seed_text="Investigate this bug", message=message))

    assert topic_context is not None
    assert topic_context.thread_id == "88"
    send_mock.assert_awaited_once()
    assert send_mock.await_args.args[0] == context


def test_should_auto_create_topic_only_for_general_topic_messages() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        message_id="77",
        platform="telegram",
        platform_specific={"chat_type": "supergroup"},
    )

    assert bot._should_auto_create_topic(context, {"is_topic_message": True}, "hello") is True
    assert bot._should_auto_create_topic(context, {"is_topic_message": True, "reply_to_message": {"message_id": 1}}, "hello") is False
    assert bot._should_auto_create_topic(context, {"is_topic_message": True}, "/start") is False


def test_should_auto_create_topic_for_forum_general_without_thread_id() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id=None,
        message_id="77",
        platform="telegram",
        platform_specific={"chat_type": "supergroup", "is_forum": True, "is_topic_message": False},
    )

    message = {
        "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum", "is_forum": True},
        "message_id": 77,
    }

    assert bot._should_auto_create_topic(context, message, "hello from general") is True


def test_should_not_auto_create_topic_for_empty_general_service_update() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id=None,
        message_id="77",
        platform="telegram",
        platform_specific={"chat_type": "supergroup", "is_forum": True, "is_topic_message": False},
    )
    message = {
        "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum", "is_forum": True},
        "message_id": 77,
        "pinned_message": {"message_id": 12},
    }

    assert bot._should_auto_create_topic(context, message, "") is False


def test_should_auto_create_topic_for_general_photo_without_text() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    message = {
        "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum", "is_forum": True},
        "message_id": 77,
        "photo": [{"file_id": "small"}, {"file_id": "large", "file_size": 42}],
    }
    context = bot._build_message_context(
        {
            **message,
            "from": {"id": 42},
        }
    )

    assert context is not None
    assert bot._should_auto_create_topic(context, message, "") is True


def test_start_new_topic_session_allows_forum_context_without_thread_id() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", forum_auto_topic=True))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id=None,
        message_id="77",
        platform="telegram",
        platform_specific={"chat_type": "supergroup", "is_forum": True, "is_topic_message": False},
    )
    message = {
        "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum", "is_forum": True},
        "from": {"first_name": "Alex"},
        "message_id": 77,
    }

    with patch(
        "modules.im.telegram.telegram_api.create_forum_topic",
        new=AsyncMock(return_value={"result": {"message_thread_id": 88}}),
    ):
        with patch.object(bot, "send_message", new=AsyncMock(return_value="1")) as send_mock:
            topic_context = asyncio.run(bot.start_new_topic_session(context, seed_text="Investigate this bug", message=message))

    assert topic_context is not None
    assert topic_context.thread_id == "88"
    send_mock.assert_awaited_once()
    assert send_mock.await_args.args[0] == context


def test_auto_topic_handoff_rechecks_authorization_before_dispatch() -> None:
    bot = TelegramBot(
        TelegramConfig(
            bot_token="123456:test-token",
            forum_auto_topic=True,
            require_mention=False,
        )
    )
    auth_thread_ids: list[str | None] = []

    def check_authorization(**kwargs):
        auth_thread_ids.append(kwargs.get("thread_id"))
        if len(auth_thread_ids) == 1:
            return SimpleNamespace(allowed=True, denial="")
        return SimpleNamespace(allowed=False, denial="unauthorized_channel")

    bot.check_authorization = check_authorization
    bot.build_auth_denial_text = lambda denial, channel_id=None: None
    bot._maybe_route_to_forum_topic = AsyncMock(
        return_value=MessageContext(
            user_id="42",
            channel_id="-100123",
            thread_id="88",
            message_id="77",
            platform="telegram",
            platform_specific={
                "is_dm": False,
                "chat_type": "supergroup",
                "is_forum": True,
                "is_topic_message": True,
            },
        )
    )
    bot._spawn_message_callback_task = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 77,
                "message_thread_id": 1,
                "is_topic_message": True,
                "chat": {
                    "id": -100123,
                    "type": "supergroup",
                    "title": "Core Forum",
                    "is_forum": True,
                },
                "from": {"id": 42},
                "text": "start a session",
            }
        )
    )

    assert auth_thread_ids == ["1", "88"]
    bot._spawn_message_callback_task.assert_not_awaited()


def test_auto_topic_handoff_rechecks_destination_mention_policy() -> None:
    bot = TelegramBot(
        TelegramConfig(
            bot_token="123456:test-token",
            forum_auto_topic=True,
            require_mention=True,
        )
    )
    settings_keys: list[str] = []

    def get_require_mention(settings_key, global_default=False):
        settings_keys.append(settings_key)
        return settings_key != "thread::-100123::1"

    bot.settings_manager = SimpleNamespace(get_require_mention=get_require_mention)
    bot.check_authorization = lambda **kwargs: SimpleNamespace(allowed=True, denial="")
    bot._maybe_route_to_forum_topic = AsyncMock(
        return_value=MessageContext(
            user_id="42",
            channel_id="-100123",
            thread_id="88",
            message_id="77",
            platform="telegram",
            platform_specific={
                "is_dm": False,
                "chat_type": "supergroup",
                "is_forum": True,
                "is_topic_message": True,
            },
        )
    )
    bot._spawn_message_callback_task = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 77,
                "message_thread_id": 1,
                "is_topic_message": True,
                "chat": {
                    "id": -100123,
                    "type": "supergroup",
                    "title": "Core Forum",
                    "is_forum": True,
                },
                "from": {"id": 42},
                "text": "start a session",
            }
        )
    )

    assert settings_keys == ["thread::-100123::1", "thread::-100123::88"]
    bot._spawn_message_callback_task.assert_not_awaited()


def test_build_message_context_records_discovered_chat(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    context = bot._build_message_context(
        {
            "message_id": 77,
            "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum"},
            "from": {"id": 42},
            "is_topic_message": True,
            "message_thread_id": 1,
        }
    )

    assert context is not None
    chats = chat_discovery.list_chats("telegram", include_private=False)
    assert len(chats) == 1
    assert chats[0].chat_id == "-100123"
    assert chats[0].metadata[chat_discovery.METADATA_IS_FORUM] is True


def test_handle_message_ignores_foreign_bot_command() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._bot_user = {"id": 1, "username": "vibe_remote_bot"}
    bot.on_message_callback = AsyncMock()
    bot.on_command_callbacks["start"] = AsyncMock()

    asyncio.run(
        bot._handle_message(
            {
                "message_id": 77,
                "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum"},
                "from": {"id": 42},
                "text": "/start@other_bot hello",
            }
        )
    )

    bot.on_command_callbacks["start"].assert_not_awaited()
    bot.on_message_callback.assert_not_awaited()


def test_run_dispatches_telegram_updates_concurrently() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    started: list[int] = []
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    poll_calls = 0

    async def fake_get_updates(_token: str, _offset=None, proxy_url=None):
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            return {"result": [{"update_id": 1}, {"update_id": 2}]}
        await asyncio.sleep(0)
        return {"result": []}

    async def fake_handle(update: dict[str, int]) -> None:
        started.append(update["update_id"])
        if update["update_id"] == 2:
            second_started.set()
            release_first.set()
            bot.stop()
        await release_first.wait()

    with patch("modules.im.telegram.telegram_api.get_me", new=AsyncMock(return_value={"result": {"username": "bot"}})):
        with patch("modules.im.telegram.telegram_api.get_updates", new=AsyncMock(side_effect=fake_get_updates)):
            with patch.object(bot, "_sync_command_menu", new=AsyncMock()) as sync_mock:
                with patch.object(bot, "_handle_update", new=fake_handle):
                    asyncio.run(asyncio.wait_for(bot._run(), timeout=0.2))

    assert second_started.is_set()
    assert started == [1, 2]
    sync_mock.assert_awaited_once()


def test_sync_command_menu_registers_localized_commands_and_menu_button() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token", proxy_url="socks5://127.0.0.1:1080"))

    with patch(
        "modules.im.telegram.telegram_api.set_my_commands",
        new=AsyncMock(return_value={"ok": True}),
    ) as commands_mock:
        with patch(
            "modules.im.telegram.telegram_api.set_chat_menu_button",
            new=AsyncMock(return_value={"ok": True}),
        ) as menu_mock:
            asyncio.run(bot._sync_command_menu())

    assert [call.kwargs["language_code"] for call in commands_mock.await_args_list] == [None, "en", "zh"]
    default_commands = commands_mock.await_args_list[0].args[1]
    chinese_commands = commands_mock.await_args_list[2].args[1]
    assert [item["command"] for item in default_commands] == [
        "start",
        "new",
        "cwd",
        "setcwd",
        "resume",
        "setup",
        "settings",
        "stop",
    ]
    assert default_commands[0]["description"] == "Open the main menu"
    assert chinese_commands[0]["description"] == "打开主菜单"
    assert all(call.kwargs["proxy_url"] == "socks5://127.0.0.1:1080" for call in commands_mock.await_args_list)
    menu_mock.assert_awaited_once_with(
        "123456:test-token",
        menu_button={"type": "commands"},
        proxy_url="socks5://127.0.0.1:1080",
    )


def test_sync_command_menu_keeps_going_after_registration_failure() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    with patch(
        "modules.im.telegram.telegram_api.set_my_commands",
        new=AsyncMock(side_effect=[RuntimeError("default failed"), {"ok": True}, {"ok": True}]),
    ) as commands_mock:
        with patch(
            "modules.im.telegram.telegram_api.set_chat_menu_button",
            new=AsyncMock(return_value={"ok": True}),
        ) as menu_mock:
            asyncio.run(bot._sync_command_menu())

    assert commands_mock.await_count == 3
    menu_mock.assert_awaited_once()


def test_set_my_commands_builds_language_payload_and_uses_proxy() -> None:
    with patch(
        "modules.im.telegram_api.call_api",
        new=AsyncMock(return_value={"ok": True}),
    ) as call_mock:
        asyncio.run(
            telegram_api.set_my_commands(
                "123456:test-token",
                [{"command": "start", "description": "Open the main menu"}],
                language_code="en",
                proxy_url="socks5://127.0.0.1:1080",
            )
        )

    call_mock.assert_awaited_once_with(
        "123456:test-token",
        "setMyCommands",
        {
            "commands": [{"command": "start", "description": "Open the main menu"}],
            "language_code": "en",
        },
        proxy_url="socks5://127.0.0.1:1080",
    )


def test_spawn_update_task_keeps_same_scope_updates_ordered() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    started: list[int] = []
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    async def fake_handle(update: dict[str, int]) -> None:
        started.append(update["update_id"])
        if update["update_id"] == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    async def scenario() -> None:
        with patch.object(bot, "_handle_update", new=fake_handle):
            bot._spawn_update_task(
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 42},
                    },
                }
            )
            await first_started.wait()
            bot._spawn_update_task(
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 42},
                    },
                }
            )
            await asyncio.sleep(0)
            assert not second_started.is_set()
            release_first.set()
            await bot._drain_background_tasks()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))

    assert second_started.is_set()
    assert started == [1, 2]


def test_scoped_update_gate_is_evicted_once_scope_turns_idle() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))

    async def scenario() -> None:
        with patch.object(bot, "_handle_update", new=AsyncMock()) as handle_mock:
            await bot._handle_scoped_update(
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 42},
                    },
                },
                "-100123:42",
            )
            handle_mock.assert_awaited_once()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))

    assert bot._update_scope_gates == {}


def test_wait_for_update_capacity_blocks_until_inflight_task_finishes() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._MAX_IN_FLIGHT_UPDATE_TASKS = 1
    release = asyncio.Event()
    blocker = asyncio.Event()

    async def long_task() -> None:
        blocker.set()
        await release.wait()

    async def scenario() -> None:
        task = asyncio.create_task(long_task())
        bot._update_tasks.add(task)
        task.add_done_callback(bot._handle_update_task_done)
        await blocker.wait()

        waiter = asyncio.create_task(bot._wait_for_update_capacity())
        await asyncio.sleep(0)
        assert not waiter.done()

        release.set()
        await waiter
        await bot._drain_background_tasks()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))


def test_drain_background_tasks_waits_for_message_callbacks() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    finished = asyncio.Event()

    async def callback(_context, _text: str) -> None:
        await asyncio.sleep(0)
        finished.set()

    async def scenario() -> None:
        bot.on_message_callback = callback
        context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
        await bot._spawn_message_callback_task(context, "hello")
        await bot._drain_background_tasks()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))

    assert finished.is_set()


def test_spawn_message_callback_task_waits_for_capacity() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS = 1
    release = asyncio.Event()
    blocker = asyncio.Event()

    async def running_callback(_context, _text: str) -> None:
        blocker.set()
        await release.wait()

    async def scenario() -> None:
        bot.on_message_callback = running_callback
        context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
        await bot._spawn_message_callback_task(context, "first")
        await blocker.wait()

        waiter = asyncio.create_task(bot._spawn_message_callback_task(context, "second"))
        await asyncio.sleep(0)
        assert not waiter.done()

        release.set()
        await waiter
        await bot._drain_background_tasks()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))


def test_spawn_message_callback_task_rechecks_capacity_after_wakeup() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS = 1
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def running_callback(_context, text: str) -> None:
        order.append(text)
        if text == "first":
            first_started.set()
            await first_release.wait()
        elif text == "second":
            second_started.set()
            await second_release.wait()

    async def scenario() -> None:
        bot.on_message_callback = running_callback
        context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
        await bot._spawn_message_callback_task(context, "first")
        await first_started.wait()

        second_waiter = asyncio.create_task(bot._spawn_message_callback_task(context, "second"))
        third_waiter = asyncio.create_task(bot._spawn_message_callback_task(context, "third"))
        await asyncio.sleep(0)
        assert not second_waiter.done()
        assert not third_waiter.done()

        first_release.set()
        await second_started.wait()
        await asyncio.sleep(0)
        assert second_waiter.done()
        assert not third_waiter.done()

        second_release.set()
        await third_waiter
        await bot._drain_background_tasks()

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.2))

    assert order == ["first", "second", "third"]


def test_pending_cwd_prompt_consumes_next_plain_message() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._cwd_prompts[bot._interaction_scope_key(context)] = SimpleNamespace(message_id="10", current_cwd="/tmp")
    bot._controller = SimpleNamespace(
        command_handler=SimpleNamespace(handle_set_cwd=AsyncMock()),
    )

    with patch.object(bot, "_delete_interaction_message", new=AsyncMock()) as delete_mock:
        handled = asyncio.run(bot._consume_cwd_prompt(context, "/repo/new"))

    assert handled is True
    bot._controller.command_handler.handle_set_cwd.assert_awaited_once()
    delete_mock.assert_awaited_once_with(context, "10")
    assert bot._interaction_scope_key(context) not in bot._cwd_prompts


def test_interaction_scope_key_separates_forum_topics() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    topic_42 = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="42",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    topic_43 = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="43",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    general = MessageContext(
        user_id="42",
        channel_id="-100123",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True},
    )

    assert bot._interaction_scope_key(topic_42) == "-100123:42:42"
    assert bot._interaction_scope_key(topic_43) == "-100123:42:43"
    assert bot._interaction_scope_key(general) == "-100123:42:1"


def test_pending_cwd_prompt_bypasses_slash_command_with_args() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._cwd_prompts[bot._interaction_scope_key(context)] = SimpleNamespace(message_id="10", current_cwd="/tmp")

    handled = asyncio.run(bot._consume_cwd_prompt(context, "/resume codex:abc"))

    assert handled is False
    assert bot._interaction_scope_key(context) in bot._cwd_prompts


def test_send_message_uses_html_parse_mode() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(return_value={"result": {"message_id": 77}}),
    ) as call_mock:
        asyncio.run(bot.send_message(context, "Hello **world**"))

    payload = call_mock.await_args.args[2]
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "Hello <b>world</b>"


def test_send_markdown_message_keeps_plain_markdown_on_html_path() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(return_value={"result": {"message_id": 77}}),
    ) as call_mock:
        asyncio.run(bot.send_markdown_message(context, "Hello **world**"))

    assert call_mock.await_args.args[1] == "sendMessage"
    payload = call_mock.await_args.args[2]
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "Hello <b>world</b>"


def test_send_markdown_message_uses_rich_message_for_structured_markdown() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", thread_id="8", platform="telegram")
    keyboard = InlineKeyboard(buttons=[[InlineButton(text="Inspect", callback_data="quick_reply:Inspect")]])
    markdown = "# Summary\n\n- shipped\n- verified"

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(return_value={"result": {"message_id": 88}}),
    ) as call_mock:
        result = asyncio.run(bot.send_markdown_message(context, markdown, keyboard=keyboard, reply_to="77"))

    assert result == "88"
    assert call_mock.await_args.args[1] == "sendRichMessage"
    payload = call_mock.await_args.args[2]
    assert payload["chat_id"] == "-100123"
    assert payload["message_thread_id"] == 8
    assert payload["reply_parameters"] == {"message_id": 77}
    assert payload["rich_message"] == {"markdown": markdown}
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "Inspect", "callback_data": "quick_reply:Inspect"}]]
    }


def test_send_markdown_message_preserves_reply_to_on_plain_button_path() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
    keyboard = InlineKeyboard(buttons=[[InlineButton(text="Inspect", callback_data="quick_reply:Inspect")]])

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(return_value={"result": {"message_id": 88}}),
    ) as call_mock:
        result = asyncio.run(bot.send_markdown_message(context, "Hello **world**", keyboard=keyboard, reply_to="77"))

    assert result == "88"
    assert call_mock.await_args.args[1] == "sendMessage"
    payload = call_mock.await_args.args[2]
    assert payload["reply_parameters"] == {"message_id": 77}
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "Inspect", "callback_data": "quick_reply:Inspect"}]]
    }
    assert payload["parse_mode"] == "HTML"


def test_send_markdown_message_falls_back_when_rich_message_is_rejected() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
    markdown = "# Summary\n\n- shipped"

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(
            side_effect=[
                RuntimeError("Bad Request: method not found"),
                {"result": {"message_id": 89}},
            ]
        ),
    ) as call_mock:
        result = asyncio.run(bot.send_markdown_message(context, markdown))

    assert result == "89"
    assert [call.args[1] for call in call_mock.await_args_list] == ["sendRichMessage", "sendMessage"]
    fallback_payload = call_mock.await_args_list[1].args[2]
    assert fallback_payload["parse_mode"] == "HTML"
    assert fallback_payload["text"] == "# Summary\n\n- shipped"


def test_send_markdown_message_retries_rich_without_remote_image_media() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
    markdown = (
        "# Summary\n\n"
        "![示例图片](https://via.placeholder.com/120x80.png?text=Markdown)\n\n"
        '![captioned](https://host/img.png "caption")\n\n'
        "![angle](<https://host/img space.png>)\n\n"
        "![upper](HTTPS://host/upper.png)\n\n"
        "![](https://host/no-alt.png)\n\n"
        "\\![escaped](https://host/escaped.png)\n\n"
        "[![badge](https://img.shields.io/x.svg)](https://example.com)\n\n"
        "- screenshot:\n"
        "    ![shot](https://host/list.png)\n\n"
        "1. ordered screenshot:\n"
        "      ![ordered](https://host/ordered.png)\n\n"
        "- sample:\n"
        "      ![literal](https://host/code.png)\n\n"
        "![diagram][img]\n\n"
        "[![refbadge](https://img.example/x.svg)][badge-url]\n\n"
        "[img]: https://host/diagram.png\n"
        "[badge-url]: https://example.com/ref\n\n"
        "`![inline](https://host/inline.png)`\n\n"
        "```md\n"
        "![fenced](https://host/fenced.png)\n"
        "```\n\n"
        "    ![indented](https://host/indented.png)\n\n"
        "- shipped"
    )

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(
            side_effect=[
                RuntimeError("Bad Request: RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND"),
                {"result": {"message_id": 89}},
            ]
        ),
    ) as call_mock:
        result = asyncio.run(bot.send_markdown_message(context, markdown))

    assert result == "89"
    assert [call.args[1] for call in call_mock.await_args_list] == ["sendRichMessage", "sendRichMessage"]
    retry_payload = call_mock.await_args_list[1].args[2]
    assert retry_payload["rich_message"] == {
        "markdown": (
            "# Summary\n\n"
            "[示例图片](https://via.placeholder.com/120x80.png?text=Markdown)\n\n"
            "[captioned](https://host/img.png)\n\n"
            "[angle](<https://host/img space.png>)\n\n"
            "[upper](HTTPS://host/upper.png)\n\n"
            "[https://host/no-alt.png](https://host/no-alt.png)\n\n"
            "\\![escaped](https://host/escaped.png)\n\n"
            "[badge](https://example.com)\n\n"
            "- screenshot:\n"
            "    [shot](https://host/list.png)\n\n"
            "1. ordered screenshot:\n"
            "      [ordered](https://host/ordered.png)\n\n"
            "- sample:\n"
            "      ![literal](https://host/code.png)\n\n"
            "[diagram](https://host/diagram.png)\n\n"
            "[refbadge][badge-url]\n\n"
            "[img]: https://host/diagram.png\n"
            "[badge-url]: https://example.com/ref\n\n"
            "`![inline](https://host/inline.png)`\n\n"
            "```md\n"
            "![fenced](https://host/fenced.png)\n"
            "```\n\n"
            "    ![indented](https://host/indented.png)\n\n"
            "- shipped"
        )
    }


def test_send_markdown_message_preserves_reply_to_on_rejected_rich_button_fallback() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
    keyboard = InlineKeyboard(buttons=[[InlineButton(text="Inspect", callback_data="quick_reply:Inspect")]])
    markdown = "# Summary\n\n- shipped"

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(
            side_effect=[
                RuntimeError("Bad Request: rich message is invalid"),
                {"result": {"message_id": 89}},
            ]
        ),
    ) as call_mock:
        result = asyncio.run(bot.send_markdown_message(context, markdown, keyboard=keyboard, reply_to="77"))

    assert result == "89"
    assert [call.args[1] for call in call_mock.await_args_list] == ["sendRichMessage", "sendMessage"]
    fallback_payload = call_mock.await_args_list[1].args[2]
    assert fallback_payload["reply_parameters"] == {"message_id": 77}
    assert fallback_payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "Inspect", "callback_data": "quick_reply:Inspect"}]]
    }


def test_send_markdown_message_disables_rich_path_after_method_missing() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")
    markdown = "# Summary\n\n- shipped"

    with patch(
        "modules.im.telegram.telegram_api.call_api",
        new=AsyncMock(
            side_effect=[
                RuntimeError("Bad Request: method not found"),
                {"result": {"message_id": 89}},
                {"result": {"message_id": 90}},
            ]
        ),
    ) as call_mock:
        first = asyncio.run(bot.send_markdown_message(context, markdown))
        second = asyncio.run(bot.send_markdown_message(context, markdown))

    assert first == "89"
    assert second == "90"
    assert [call.args[1] for call in call_mock.await_args_list] == [
        "sendRichMessage",
        "sendMessage",
        "sendMessage",
    ]


def test_add_reaction_uses_telegram_message_reactions() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")

    with patch(
        "modules.im.telegram.telegram_api.set_message_reaction",
        new=AsyncMock(return_value={"ok": True}),
    ) as reaction_mock:
        result = asyncio.run(bot.add_reaction(context, "77", ":eyes:"))

    assert result is True
    reaction_mock.assert_awaited_once_with("123456:test-token", "-100123", "77", "👀", proxy_url=None)


def test_remove_reaction_clears_telegram_message_reactions() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(user_id="42", channel_id="-100123", platform="telegram")

    with patch(
        "modules.im.telegram.telegram_api.clear_message_reaction",
        new=AsyncMock(return_value={"ok": True}),
    ) as reaction_mock:
        result = asyncio.run(bot.remove_reaction(context, "77", ":eyes:"))

    assert result is True
    reaction_mock.assert_awaited_once_with("123456:test-token", "-100123", "77", proxy_url=None)


def test_resume_menu_uses_short_callback_ids() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    sessions = [
        NativeResumeSession(
            agent="codex",
            agent_prefix="cx",
            native_session_id="session_abcdefghijklmnopqrstuvwxyz",
            working_path="/Users/alice/avibe",
            created_at=None,
            updated_at=None,
            sort_ts=100.0,
            last_agent_message="Latest answer",
            last_agent_tail="...Latest answer",
        )
    ]

    with patch.object(bot, "send_message_with_buttons", new=AsyncMock(return_value="55")) as send_mock:
        asyncio.run(
            bot.open_resume_session_modal(
                context,
                sessions,
                context.channel_id,
                context.thread_id,
                context.message_id,
            )
        )

    text = send_mock.await_args.args[1]
    keyboard = send_mock.await_args.args[2]
    assert keyboard.buttons[0][0].callback_data == "tg_resume:0"
    state = bot._resume_states[bot._interaction_scope_key(context)]
    assert state.options == [("codex", "session_abcdefghijklmnopqrstuvwxyz")]
    assert state.thread_id == "1"
    assert "cx...Latest answer" in text


def test_resume_callback_submits_selected_session() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="55",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True},
    )
    bot._resume_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="55",
        options=[("claude", "sess_123")],
        is_dm=False,
        thread_id="1",
    )
    bot._controller = SimpleNamespace(
        session_handler=SimpleNamespace(handle_resume_session_submission=AsyncMock()),
    )

    with patch.object(bot, "edit_message", new=AsyncMock(return_value=True)):
        asyncio.run(bot._handle_resume_callback(context, "tg_resume:0"))

    bot._controller.session_handler.handle_resume_session_submission.assert_awaited_once_with(
        user_id="42",
        channel_id="-100123",
        thread_id="1",
        agent="claude",
        session_id="sess_123",
        is_dm=False,
        platform="telegram",
    )


def test_routing_callback_save_persists_selected_backend() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="88",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._routing_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="88",
        channel_id=context.channel_id,
        user_id=context.user_id,
        is_dm=False,
        backend="codex",
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent=None,
        claude_model=None,
        claude_reasoning_effort=None,
        codex_model="gpt-5",
        codex_reasoning_effort="high",
        picker_field=None,
        picker_page=0,
    )
    bot._controller = SimpleNamespace(
        settings_handler=SimpleNamespace(handle_routing_update=AsyncMock()),
    )

    with patch.object(bot, "_delete_interaction_message", new=AsyncMock()) as delete_mock:
        asyncio.run(bot._handle_routing_callback(context, "tg_route:save"))

    delete_mock.assert_awaited_once_with(context, "88")
    bot._controller.settings_handler.handle_routing_update.assert_awaited_once()


def test_routing_state_marks_current_backend_in_first_row() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    state = SimpleNamespace(
        registered_backends=["opencode", "claude", "codex"],
        backend="claude",
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent="reviewer",
        claude_model="claude-sonnet-4-6",
        claude_reasoning_effort="high",
        codex_model=None,
        codex_reasoning_effort=None,
        picker_field=None,
        picker_page=0,
    )

    _, keyboard = bot._render_routing_state(state)

    assert [button.callback_data for button in keyboard.buttons[0]] == [
        "tg_route:backend:opencode",
        "tg_route:backend:claude",
        "tg_route:backend:codex",
    ]
    assert keyboard.buttons[0][1].text.startswith("☑️ ")


def test_routing_codex_reasoning_uses_shared_catalog_options() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    state = SimpleNamespace(
        picker_field="codex_reasoning_effort",
        codex_model="gpt-5.6-terra",
        backend_reasoning_options={
            "codex": {
                "gpt-5.6-terra": [
                    {"value": "__default__", "label": "(Default)"},
                    {"value": "ultra", "label": "Ultra"},
                ]
            }
        },
    )

    assert bot._routing_picker_options(state) == [
        (bot._t("common.default"), None),
        ("Ultra", "ultra"),
    ]


def test_open_routing_modal_clears_stale_catalog_effort() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="88",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    current_routing = SimpleNamespace(
        model="gpt-5.6-terra",
        reasoning_effort="minimal",
        codex_agent=None,
    )

    with patch.object(bot, "send_message_with_buttons", new=AsyncMock(return_value="99")):
        asyncio.run(
            bot.open_routing_modal(
                context,
                context.channel_id,
                current_routing=current_routing,
                current_backend="codex",
                registered_backends=["codex"],
                codex_models=["gpt-5.6-terra"],
                backend_reasoning_options={
                    "codex": {
                        "gpt-5.6-terra": [
                            {"value": "__default__", "label": "(Default)"},
                            {"value": "ultra", "label": "Ultra"},
                        ]
                    }
                },
            )
        )

    state = bot._routing_states[bot._interaction_scope_key(context)]
    assert state.codex_reasoning_effort is None


def test_routing_callback_backend_switches_without_nested_picker() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="88",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._routing_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="88",
        channel_id=context.channel_id,
        user_id=context.user_id,
        is_dm=False,
        registered_backends=["opencode", "claude", "codex"],
        backend="opencode",
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent=None,
        claude_model=None,
        claude_reasoning_effort=None,
        codex_model=None,
        codex_reasoning_effort=None,
        picker_field=None,
        picker_page=0,
    )

    with patch.object(bot, "edit_message", new=AsyncMock(return_value=True)) as edit_mock:
        asyncio.run(bot._handle_routing_callback(context, "tg_route:backend:claude"))

    state = bot._routing_states[bot._interaction_scope_key(context)]
    assert state.backend == "claude"
    assert state.picker_field is None
    edit_mock.assert_awaited_once()


def test_routing_state_keeps_backend_picker_entry_for_extra_backends() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    state = SimpleNamespace(
        registered_backends=["opencode", "claude", "codex", "extra"],
        backend="extra",
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent=None,
        claude_model=None,
        claude_reasoning_effort=None,
        codex_model=None,
        codex_reasoning_effort=None,
        picker_field=None,
        picker_page=0,
    )

    _, keyboard = bot._render_routing_state(state)

    assert [button.callback_data for button in keyboard.buttons[0]] == [
        "tg_route:backend:opencode",
        "tg_route:backend:claude",
        "tg_route:backend:codex",
    ]
    assert keyboard.buttons[1][0].callback_data == "tg_route:field:backend"


def test_routing_callback_backend_picker_can_select_extra_backend() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="88",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._routing_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="88",
        channel_id=context.channel_id,
        user_id=context.user_id,
        is_dm=False,
        registered_backends=["opencode", "claude", "codex", "extra"],
        backend="opencode",
        opencode_agent=None,
        opencode_model=None,
        opencode_reasoning_effort=None,
        claude_agent=None,
        claude_model=None,
        claude_reasoning_effort=None,
        codex_model=None,
        codex_reasoning_effort=None,
        picker_field=None,
        picker_page=0,
    )

    with patch.object(bot, "edit_message", new=AsyncMock(return_value=True)) as edit_mock:
        asyncio.run(bot._handle_routing_callback(context, "tg_route:field:backend"))
        asyncio.run(bot._handle_routing_callback(context, "tg_route:option:3"))

    state = bot._routing_states[bot._interaction_scope_key(context)]
    assert state.backend == "extra"
    assert state.picker_field is None
    assert edit_mock.await_count == 2


def test_open_settings_modal_includes_language_buttons() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="88",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    user_settings = SimpleNamespace(show_message_types=["assistant", "toolcall"])

    with patch.object(bot, "send_message_with_buttons", new=AsyncMock(return_value="66")) as send_mock:
        asyncio.run(
            bot.open_settings_modal(
                context,
                user_settings=user_settings,
                message_types=["assistant", "toolcall", "system"],
                display_names={},
                channel_id=context.channel_id,
                current_require_mention=None,
                global_require_mention=True,
                current_language="en",
            )
        )

    keyboard = send_mock.await_args.args[2]
    language_row = keyboard.buttons[-3]
    assert [button.callback_data for button in language_row] == ["tg_settings:lang:en", "tg_settings:lang:zh"]


def test_render_settings_state_localizes_current_label() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot._controller = SimpleNamespace(_get_lang=lambda: "en")

    text, _ = bot._render_settings_state(
        SimpleNamespace(
            show_message_types=["assistant"],
            current_require_mention=None,
            global_require_mention=True,
            current_language="en",
        ),
        ["assistant", "toolcall"],
    )

    assert "Current:" in text
    assert "当前:" not in text


def test_settings_callback_save_updates_language_and_deletes_menu() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="66",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot.settings_manager = SimpleNamespace(get_available_message_types=lambda: ["assistant", "toolcall", "system"])
    bot._settings_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="66",
        show_message_types=["assistant"],
        current_require_mention=None,
        global_require_mention=True,
        current_language="zh",
        is_dm=False,
        thread_id="1",
    )
    bot._controller = SimpleNamespace(
        settings_handler=SimpleNamespace(handle_settings_update=AsyncMock()),
    )

    with patch.object(bot, "_delete_interaction_message", new=AsyncMock()) as delete_mock:
        asyncio.run(bot._handle_settings_callback(context, "tg_settings:save"))

    delete_mock.assert_awaited_once_with(context, "66")
    bot._controller.settings_handler.handle_settings_update.assert_awaited_once_with(
        user_id="42",
        show_message_types=["assistant"],
        channel_id="-100123",
        require_mention=None,
        language="zh",
        notify_user=True,
        is_dm=False,
        platform="telegram",
        thread_id="1",
    )


def test_callback_context_preserves_general_forum_identity() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot.check_authorization = lambda **kwargs: SimpleNamespace(allowed=True, denial=None)
    captured_contexts: list[MessageContext] = []

    async def handle_internal(context: MessageContext, callback_data: str) -> bool:
        captured_contexts.append(context)
        return True

    bot._handle_internal_callback = handle_internal

    with patch.object(bot, "answer_callback", new=AsyncMock(return_value=True)):
        asyncio.run(
            bot._handle_callback_query(
                {
                    "id": "cb-general",
                    "data": "tg_settings:save",
                    "from": {"id": 42},
                    "message": {
                        "message_id": 66,
                        "is_topic_message": True,
                        "chat": {
                            "id": -100123,
                            "type": "supergroup",
                            "title": "Core Forum",
                            "is_forum": True,
                        },
                    },
                }
            )
        )

    assert len(captured_contexts) == 1
    assert resolve_context_thread_id(captured_contexts[0]) == "1"
    assert bot._interaction_scope_key(captured_contexts[0]) == "-100123:42:1"


def test_open_question_modal_edits_message_with_telegram_buttons() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="99",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    pending = SimpleNamespace(
        questions=[
            SimpleNamespace(
                header="Backend",
                question="Pick a backend",
                options=[SimpleNamespace(label="Codex", description=""), SimpleNamespace(label="Claude", description="")],
                multiple=False,
            ),
            SimpleNamespace(
                header="Reasoning",
                question="Pick reasoning",
                options=[SimpleNamespace(label="High", description="")],
                multiple=False,
            ),
        ]
    )

    with patch.object(bot, "edit_message", new=AsyncMock(return_value=True)) as edit_mock:
        asyncio.run(bot.open_question_modal(context, context, pending, "claude_question"))

    assert bot._question_states[bot._interaction_scope_key(context)].message_id == "99"
    keyboard = edit_mock.await_args.kwargs["keyboard"]
    assert keyboard.buttons[0][0].callback_data == "tg_question:choose:1"


def test_question_callback_finalizes_with_synthetic_modal_payload() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    context = MessageContext(
        user_id="42",
        channel_id="-100123",
        message_id="99",
        platform="telegram",
        platform_specific={"is_dm": False},
    )
    bot._question_states[bot._interaction_scope_key(context)] = SimpleNamespace(
        message_id="99",
        callback_prefix="claude_question",
        questions=[
            SimpleNamespace(
                header="Backend",
                question="Pick a backend",
                options=[SimpleNamespace(label="Codex", description=""), SimpleNamespace(label="Claude", description="")],
                multiple=False,
            )
        ],
        answers=[[]],
        index=0,
    )
    bot.on_callback_query_callback = AsyncMock()

    with patch.object(bot, "edit_message", new=AsyncMock(return_value=True)):
        asyncio.run(bot._handle_question_callback(context, "tg_question:choose:2"))

    bot.on_callback_query_callback.assert_awaited_once()
    forwarded = bot.on_callback_query_callback.await_args.args[1]
    assert forwarded.startswith("claude_question:modal:")
    assert '"Claude"' in forwarded


def test_handle_callback_query_denies_unauthorized_protected_action() -> None:
    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    bot.check_authorization = lambda **kwargs: SimpleNamespace(allowed=False, denial="not_admin")
    bot.build_auth_denial_text = lambda denial, channel_id=None: "Admin only"
    bot.on_callback_query_callback = AsyncMock()

    with patch.object(bot, "answer_callback", new=AsyncMock(return_value=True)) as answer_mock:
        asyncio.run(
            bot._handle_callback_query(
                {
                    "id": "cb-1",
                    "data": "cmd_settings",
                    "from": {"id": 42},
                    "message": {
                        "message_id": 99,
                        "chat": {"id": -100123, "type": "supergroup", "title": "Core Forum"},
                    },
                }
            )
        )

    answer_mock.assert_awaited_once_with("cb-1", "Admin only", show_alert=True)
    bot.on_callback_query_callback.assert_not_awaited()


def test_remove_client_cancels_telegram_long_poll_promptly(monkeypatch) -> None:
    poll_started = threading.Event()
    poll_cancelled = threading.Event()

    async def _get_me(*args, **kwargs):
        return {"result": {"id": 1, "username": "vibe_remote_bot"}}

    async def _get_updates(*args, **kwargs):
        poll_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            poll_cancelled.set()
            raise

    monkeypatch.setattr("modules.im.telegram.telegram_api.get_me", _get_me)
    monkeypatch.setattr("modules.im.telegram.telegram_api.get_updates", _get_updates)

    bot = TelegramBot(TelegramConfig(bot_token="123456:test-token"))
    client = MultiIMClient({"telegram": bot}, primary_platform="telegram")
    runtime_thread = threading.Thread(target=bot.run, daemon=True)
    runtime_thread.start()
    client._threads["telegram"] = runtime_thread

    assert poll_started.wait(timeout=2)

    started = time.monotonic()
    removed = client.remove_client("telegram")
    elapsed = time.monotonic() - started

    assert removed is bot
    assert elapsed < 2
    assert runtime_thread.is_alive() is False
    assert poll_cancelled.is_set()
