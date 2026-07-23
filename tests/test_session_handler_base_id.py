from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handlers.session_handler import ClaudeSessionNotFoundError, SessionHandler
from modules.im import MessageContext


@dataclass
class _Config:
    platform: str = "discord"


class _FakeSessions:
    def __init__(self) -> None:
        self.alias_calls = []
        self.cross_scope_alias_calls = []
        self.clear_calls = []
        self.thread_marks = []
        self.alias_result = True
        self.cross_scope_alias_result = True
        self.clear_result = 1

    def alias_session_base(self, user_id, source_base_session_id, alias_base_session_id):
        self.alias_calls.append((user_id, source_base_session_id, alias_base_session_id))
        return self.alias_result

    def clear_session_base(self, user_id, base_session_id):
        self.clear_calls.append((user_id, base_session_id))
        return self.clear_result

    def alias_session_base_across_scopes(
        self,
        source_user_id,
        target_user_id,
        source_base_session_id,
        alias_base_session_id,
    ):
        self.cross_scope_alias_calls.append(
            (source_user_id, target_user_id, source_base_session_id, alias_base_session_id)
        )
        return self.cross_scope_alias_result

    def mark_thread_active(self, user_id, channel_id, thread_ts):
        self.thread_marks.append((user_id, channel_id, thread_ts))


class _Controller:
    def __init__(
        self,
        *,
        platform: str = "discord",
        dm_threads: bool = False,
        channel_message_sessions: bool = True,
    ) -> None:
        self.config = _Config()
        self.config.platform = platform
        self.sessions = _FakeSessions()
        self.im_client = type(
            "IM",
            (),
            {
                "formatter": None,
                "should_use_thread_for_dm_session": lambda self: dm_threads,
                "should_use_message_id_for_channel_session": lambda self, context=None: channel_message_sessions,
                "should_use_thread_for_reply": lambda self: platform in {"discord", "slack", "lark"},
            },
        )()
        self.settings_manager = type("Settings", (), {"sessions": None})()
        self.session_manager = object()
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}

    def get_cwd(self, context: MessageContext) -> str:
        return "/tmp/workdir"

    def _get_settings_key(self, context: MessageContext) -> str:
        return context.user_id if (context.platform_specific or {}).get("is_dm") else context.channel_id

    def _get_session_key(self, context: MessageContext) -> str:
        return f"{getattr(context, 'platform', None) or 'test'}::{self._get_settings_key(context)}"

    def get_im_client_for_context(self, context: MessageContext):
        return self.im_client


class _FakeFormatter:
    @staticmethod
    def format_error(text: str) -> str:
        return f"ERR:{text}"


class _FakeIM:
    def __init__(self) -> None:
        self.formatter = _FakeFormatter()
        self.sent_messages = []

    @staticmethod
    def should_use_thread_for_dm_session() -> bool:
        return False

    @staticmethod
    def should_use_message_id_for_channel_session(context=None) -> bool:
        return True

    @staticmethod
    def should_use_thread_for_reply() -> bool:
        return True

    async def send_message(self, context: MessageContext, message: str) -> None:
        self.sent_messages.append((context, message))


def test_dm_session_base_id_uses_stable_channel_id() -> None:
    handler = SessionHandler(_Controller(platform="discord", dm_threads=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        thread_id="thread-999",
        message_id="msg-999",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "discord_dm-123"


def test_dm_session_base_id_uses_thread_when_platform_supports_dm_threads() -> None:
    handler = SessionHandler(_Controller(platform="lark", dm_threads=True))
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        thread_id="thread-999",
        message_id="msg-999",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "lark_thread-999"


def test_base_session_id_prefers_context_platform_over_primary_config() -> None:
    handler = SessionHandler(_Controller(platform="slack", dm_threads=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="wx-123",
        platform="wechat",
        message_id="msg-42",
        platform_specific={"is_dm": False},
    )

    assert handler.get_base_session_id(context) == "wechat_msg-42"


def test_slack_dm_session_base_id_uses_thread_when_supported() -> None:
    handler = SessionHandler(_Controller(platform="slack", dm_threads=True))
    context = MessageContext(
        user_id="u-1",
        channel_id="D123",
        thread_id="171717.999",
        message_id="171717.111",
        platform_specific={"is_dm": True},
    )

    assert handler.get_base_session_id(context) == "slack_171717.999"


def test_channel_session_base_id_keeps_thread_or_message_behavior() -> None:
    handler = SessionHandler(_Controller())
    context = MessageContext(
        user_id="u-1",
        channel_id="chan-123",
        message_id="msg-999",
        platform_specific={"is_dm": False},
    )

    assert handler.get_base_session_id(context) == "discord_msg-999"


def test_telegram_plain_group_session_base_id_uses_stable_channel_id() -> None:
    handler = SessionHandler(_Controller(platform="telegram", channel_message_sessions=False))
    context = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "chat_type": "supergroup"},
    )

    assert handler.get_base_session_id(context) == "telegram_-100123"


def test_telegram_general_topic_session_base_id_includes_chat_and_canonical_topic() -> None:
    handler = SessionHandler(_Controller(platform="telegram", channel_message_sessions=False))
    first = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )
    follow_up = MessageContext(
        user_id="u-1",
        channel_id="-100123",
        message_id="43",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )

    other_forum = MessageContext(
        user_id="u-1",
        channel_id="-100999",
        message_id="42",
        platform="telegram",
        platform_specific={"is_dm": False, "is_forum": True, "is_topic_message": True},
    )

    assert handler.get_base_session_id(first) == "telegram_-100123_1"
    assert handler.get_base_session_id(follow_up) == "telegram_-100123_1"
    assert handler.get_base_session_id(other_forum) == "telegram_-100999_1"


def test_scheduled_channel_session_uses_provisional_anchor_on_threaded_surfaces() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"is_dm": False, "turn_source": "scheduled"},
    )

    base_session_id = handler.get_base_session_id(context, source="scheduled")

    assert base_session_id.startswith("slack_scheduled-")


def test_scheduled_telegram_group_session_reuses_channel_scope() -> None:
    controller = _Controller(platform="telegram", dm_threads=False, channel_message_sessions=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="-100123",
        platform="telegram",
        platform_specific={"is_dm": False, "chat_type": "supergroup", "turn_source": "scheduled"},
    )

    assert handler.get_base_session_id(context, source="scheduled") == "telegram_-100123"


def test_scheduled_dm_session_reuses_flat_session_scope() -> None:
    controller = _Controller(platform="discord", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="u-1",
        channel_id="dm-123",
        platform="discord",
        platform_specific={"is_dm": True, "turn_source": "scheduled"},
    )

    assert handler.get_base_session_id(context, source="scheduled") == "discord_dm-123"


def test_finalize_scheduled_delivery_aliases_provisional_base_and_marks_thread() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_scheduled-abc",
            "delivery_override": {"channel_id": "C123"},
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "slack::C123",
                "clear_source": True,
            },
        },
    )

    handler.finalize_scheduled_delivery(context, "171717.123")

    assert controller.sessions.alias_calls == [("slack::C123", "slack_scheduled-abc", "slack_171717.123")]
    assert controller.sessions.clear_calls == [("slack::C123", "slack_scheduled-abc")]
    assert controller.sessions.thread_marks == [("scheduled", "C123", "171717.123")]


def test_finalize_scheduled_delivery_can_alias_into_delivery_scope() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        thread_id="171717.123",
        platform_specific={
            "is_dm": False,
            "turn_source": "scheduled",
            "turn_base_session_id": "slack_171717.123",
            "scheduled_delivery_alias": {
                "mode": "sent_message",
                "session_key": "slack::C999",
                "clear_source": False,
            },
            "delivery_override": {"channel_id": "C999"},
        },
    )

    handler.finalize_scheduled_delivery(context, "181818.456")

    assert controller.sessions.alias_calls == []
    assert controller.sessions.cross_scope_alias_calls == [
        ("slack::C123", "slack::C999", "slack_171717.123", "slack_181818.456")
    ]
    assert controller.sessions.clear_calls == []
    assert controller.sessions.thread_marks == [("scheduled", "C999", "181818.456")]


def test_alias_session_base_clears_source_even_when_alias_already_exists() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.sessions.alias_result = False
    handler = SessionHandler(controller)
    context = MessageContext(
        user_id="scheduled",
        channel_id="C123",
        platform="slack",
        platform_specific={"is_dm": False},
    )

    changed = handler.alias_session_base(
        context,
        source_base_session_id="slack_scheduled-abc",
        alias_base_session_id="slack_171717.123",
        clear_source=True,
    )

    assert changed is True
    assert controller.sessions.alias_calls == [("slack::C123", "slack_scheduled-abc", "slack_171717.123")]
    assert controller.sessions.clear_calls == [("slack::C123", "slack_scheduled-abc")]


def test_claude_session_not_found_error_is_reported_without_cleanup() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    cleanup_calls = []

    async def _cleanup_session(composite_key: str) -> None:
        cleanup_calls.append(composite_key)

    handler.cleanup_session = _cleanup_session
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            "slack_C123:/tmp/other",
            context,
            ClaudeSessionNotFoundError(
                session_id="11111111-1111-1111-1111-111111111111",
                working_path="/tmp/other",
            ),
        )
    )

    assert cleanup_calls == []
    assert len(controller.im_client.sent_messages) == 1
    _, message = controller.im_client.sent_messages[0]
    assert message.startswith("ERR:Claude Code could not find the historical session")
    assert "11111111-1111-1111-1111-111111111111" in message
    assert "/tmp/other" in message


def test_claude_sdk_buffer_error_cleans_up_broken_session() -> None:
    controller = _Controller(platform="slack", dm_threads=False)
    controller.im_client = _FakeIM()
    handler = SessionHandler(controller)
    cleanup_calls = []

    async def _cleanup_session(composite_key: str, *, current_receiver_task=None) -> None:
        cleanup_calls.append((composite_key, current_receiver_task))

    handler.cleanup_session = _cleanup_session
    context = MessageContext(user_id="U123", channel_id="C123", platform="slack")

    asyncio.run(
        handler.handle_session_error(
            "slack_C123:/tmp/workdir",
            context,
            RuntimeError("Failed to decode JSON: JSON message exceeded maximum buffer size of 1048576 bytes"),
        )
    )

    assert len(cleanup_calls) == 1
    cleanup_key, cleanup_task = cleanup_calls[0]
    assert cleanup_key == "slack_C123:/tmp/workdir"
    assert cleanup_task is not None
    assert len(controller.im_client.sent_messages) == 1
    _, message = controller.im_client.sent_messages[0]
    assert message == "ERR:Connection to Claude was lost. Please try your message again."
