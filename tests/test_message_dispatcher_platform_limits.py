from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import ConsolidatedMessageDispatcher
from modules.im import MessageContext


class _StubIMClient:
    def __init__(self, max_bytes: int | None = None, *, supports_editing: bool = True):
        self.sent = []
        self.edit_calls = []
        self._next_id = 1
        self._max_bytes = max_bytes
        self._supports_editing = supports_editing

    def should_use_thread_for_reply(self):
        return False

    def supports_message_editing(self, context=None):
        return self._supports_editing

    async def send_message(self, context, text, parse_mode=None, reply_to=None):
        if self._max_bytes is not None and len(text.encode("utf-8")) > self._max_bytes:
            raise RuntimeError("message too large")
        self.sent.append((context.channel_id, context.thread_id, text, parse_mode))
        message_id = f"bot-msg-{self._next_id}"
        self._next_id += 1
        return message_id

    async def edit_message(self, context, message_id, text=None, keyboard=None, parse_mode=None):
        self.edit_calls.append((context.channel_id, context.thread_id, message_id, text, parse_mode))
        return False


class _StubSettingsManager:
    def _canonicalize_message_type(self, message_type):
        return message_type

    def is_message_type_hidden(self, settings_key, canonical_type):
        return False


class _StubSessionHandler:
    def __init__(self):
        self.calls = []

    def finalize_scheduled_delivery(self, context, sent_message_id):
        self.calls.append((context.channel_id, context.thread_id, sent_message_id))


class _StubController:
    def __init__(self, platform: str, *, max_bytes: int | None = None):
        self.config = type("Config", (), {"platform": platform, "reply_enhancements": False})()
        self.session_handler = _StubSessionHandler()
        self.im_client = _StubIMClient(max_bytes=max_bytes, supports_editing=platform != "wechat")

    def _get_settings_key(self, context):
        return context.channel_id

    def _get_session_key(self, context):
        return f"{context.platform}::{context.channel_id}"

    def get_settings_manager_for_context(self, context):
        return _StubSettingsManager()

    def get_im_client_for_context(self, context):
        return self.im_client


class MessageDispatcherPlatformLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_long_result_splits_across_messages(self):
        controller = _StubController("wechat", max_bytes=1900)
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="wechat-user", channel_id="wechat-user", platform="wechat")
        long_text = "你" * 1900

        message_id = await dispatcher.emit_agent_message(context, "result", long_text)

        self.assertEqual(message_id, "bot-msg-1")
        self.assertGreater(len(controller.im_client.sent), 1)
        self.assertEqual("".join(text for _, _, text, _ in controller.im_client.sent), long_text)
        self.assertTrue(all(len(text.encode("utf-8")) <= 1900 for _, _, text, _ in controller.im_client.sent))

    async def test_wechat_long_assistant_log_message_splits_before_send_failure(self):
        controller = _StubController("wechat")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="wechat-user", channel_id="wechat-user", platform="wechat")
        long_text = "x" * 5000

        message_id = await dispatcher.emit_agent_message(context, "assistant", long_text)

        self.assertIsNotNone(message_id)
        self.assertGreater(len(controller.im_client.sent), 1)
        self.assertTrue(all(len(text.encode("utf-8")) <= 1900 for _, _, text, _ in controller.im_client.sent))
        self.assertEqual(controller.im_client.edit_calls, [])

    async def test_wechat_non_toolcall_log_messages_send_individually_without_append_edit(self):
        controller = _StubController("wechat")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="wechat-user", channel_id="wechat-user", platform="wechat")

        first_id = await dispatcher.emit_agent_message(context, "system", "cwd: /tmp/project")
        second_id = await dispatcher.emit_agent_message(context, "assistant", "running command")

        self.assertEqual((first_id, second_id), ("bot-msg-1", "bot-msg-2"))
        self.assertEqual(
            [text for _, _, text, _ in controller.im_client.sent],
            ["cwd: /tmp/project", "running command"],
        )
        self.assertEqual(controller.im_client.edit_calls, [])

    async def test_wechat_toolcall_is_persisted_but_never_delivered(self):
        controller = _StubController("wechat")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(user_id="wechat-user", channel_id="wechat-user", platform="wechat")

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(context, "toolcall", "exec_command")

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent, [])
        persist.assert_called_once()
        self.assertEqual(persist.call_args.args[1], "toolcall")

    async def test_toolcall_delivery_override_to_wechat_is_persisted_but_never_delivered(self):
        controller = _StubController("slack")
        dispatcher = ConsolidatedMessageDispatcher(controller)
        context = MessageContext(
            user_id="U1",
            channel_id="C1",
            platform="slack",
            platform_specific={
                "delivery_override": {
                    "platform": "wechat",
                    "user_id": "wechat-user",
                    "channel_id": "wechat-user",
                }
            },
        )

        with mock.patch("core.message_dispatcher.persist_agent_message") as persist:
            message_id = await dispatcher.emit_agent_message(context, "toolcall", "exec_command")

        self.assertIsNone(message_id)
        self.assertEqual(controller.im_client.sent, [])
        persist.assert_called_once()
        persisted_context = persist.call_args.args[0]
        self.assertEqual(persisted_context.platform, "wechat")
        self.assertEqual(persisted_context.channel_id, "wechat-user")
        self.assertEqual(persist.call_args.args[1], "toolcall")

    def test_result_split_boundary_never_exceeds_platform_limit(self):
        dispatcher = ConsolidatedMessageDispatcher(_StubController("wechat"))

        chunks = dispatcher._split_result_text_by_bytes("你" * 633 + "你", 1900)

        self.assertEqual(chunks, ["你" * 633, "你"])
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 1900 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
